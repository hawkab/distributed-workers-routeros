<?php
declare(strict_types=1);
// The signed bootstrap selects the authenticated runtime and retains its lock.
// Reuse that live runtime between tasks: re-executing it on every empty poll
// unnecessarily rehashes executable programs and reconnects the same identity.
require (getenv('WORKER_BOOTSTRAP') ?: '/opt/fleet/php/bootstrap.php');
if (!isset($worker, $updater, $config) || !is_object($worker) || !method_exists($worker, 'tick')) {
    // Older/custom bootstraps still retain the original one-tick contract.
    exit(($busy ?? false) ? 76 : 0);
}
if (isset($worker->client) && property_exists($worker->client, 'waitApplied')) { $worker->client->config['claim_wait_seconds'] = 20; }
$pending = false;
$state = $config['state_directory'];
$stop = getenv('WORKER_STOP_FILE') ?: '';
$shouldStop = static function () use ($state, $stop): bool {
    clearstatcache();
    return is_file($state . '/maintenance') || ($stop !== '' && is_file($stop));
};
try {
    while (!$shouldStop()) {
        if (!$busy && ($pending || (!isset($worker->client) || !property_exists($worker->client, 'waitApplied')))) {
            $until = microtime(true) + ($config['poll_seconds'] ?? 5);
            while (microtime(true) < $until && !$shouldStop()) {
                usleep((int)min(100000, max(1, ($until - microtime(true)) * 1000000)));
            }
        }
        if ($shouldStop()) { break; }
        try {
            $pending = false;
            $busy = $worker->tick();
            if (time() >= ($worker->store->data['update_check_at'] ?? 0) + ($config['update_poll_seconds'] ?? 900)) {
                $worker->store->data['update_check_at'] = time();
                $worker->store->save();
                $update = $updater->check();
                if ($update['state'] === 'installed') { exit(75); }
            }
        } catch (FleetPending $error) {
            fwrite(STDERR, json_encode(['event' => 'worker_pending', 'code' => $error->getMessage()]) . "\n");
            if ($error->getMessage() === 'actual_host_window_yield') { exit(0); }
            $busy = false; $pending = true;
        }
    }
} catch (Throwable $error) {
    $message = $error->getMessage();
    $code = preg_match('/^[a-z][a-z0-9_]*$/D', $message) ? $message : get_class($error);
    fwrite(STDERR, json_encode(['event' => 'worker_stopped', 'code' => $code]) . "\n");
    exit(1);
}
