<?php
declare(strict_types=1);
// One durable SDK tick per child. PID 1 drains the current tick on stop;
// it never forwards a stop signal to a healthy calculation or starts another.
if (PHP_SAPI !== 'cli' || !extension_loaded('pcntl')) { exit(2); }
$config = $argv[1] ?? '/config/worker.local.json';
if (!is_file($config)) { fwrite(STDERR, "{\"event\":\"configuration_required\"}\n"); exit(2); }
$settings = json_decode(file_get_contents($config), true, 512, JSON_THROW_ON_ERROR);
$state = $settings['state_directory'] ?? '/state';
$maintenance = $state . '/maintenance';
$drained = $state . '/adapter-drained';
$stopFile = $state . '/adapter-stop-' . getmypid();
if (is_file($stopFile) && !unlink($stopFile)) { throw new RuntimeException('stop_state_unavailable'); }
$stopping = false;
pcntl_async_signals(true);
foreach ([SIGTERM, SIGINT] as $signal) {
    pcntl_signal($signal, static function () use (&$stopping, $stopFile): void {
        $stopping = true;
        if (file_put_contents($stopFile, "stop\n") === false) { fwrite(STDERR, "{\"event\":\"stop_state_unavailable\"}\n"); }
    });
}
$tick = __DIR__ . '/tick.php';
while (!$stopping) {
    clearstatcache(true, $maintenance); clearstatcache(true, $drained);
    if (is_file($maintenance)) {
        // The current tick has ended. Persist a barrier across RouterOS restarts,
        // which can otherwise force-stop work when container settings change.
        if (!is_file($drained) && file_put_contents($drained, "drained\n") === false) { throw new RuntimeException('maintenance_state_unavailable'); }
        sleep(1); continue;
    }
    if (is_file($drained) && !unlink($drained)) { throw new RuntimeException('maintenance_state_unavailable'); }
    $pid = pcntl_fork();
    if ($pid === -1) { fwrite(STDERR, "{\"event\":\"fork_unavailable\"}\n"); exit(1); }
    if ($pid === 0) {
        foreach ([SIGTERM, SIGINT] as $signal) { pcntl_signal($signal, SIG_DFL); }
        pcntl_exec(PHP_BINARY, [$tick, $config, '--once'], array_merge(getenv(), ['WORKER_STOP_FILE' => $stopFile]));
        exit(127);
    }
    do { $waited = pcntl_waitpid($pid, $status); } while ($waited === -1 && pcntl_get_last_error() === PCNTL_EINTR);
    if ($waited !== $pid) { fwrite(STDERR, "{\"event\":\"child_wait_failed\"}\n"); exit(1); }
    if ($stopping) { break; }
    clearstatcache(true, $maintenance);
    if (is_file($maintenance)) { continue; }
    $code = pcntl_wifexited($status) ? pcntl_wexitstatus($status) : 1;
    // An installed signed runtime is immediately selected on the next tick.
    if ($code === 75 || $code === 76) { continue; }
    // Poll / transport backoff is interruptible; it is not a task deadline.
    sleep(5);
}
if (is_file($stopFile)) { unlink($stopFile); }
