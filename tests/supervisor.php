<?php
declare(strict_types=1);
$root = '/tmp/worker-supervisor-test-' . bin2hex(random_bytes(8));
mkdir($root, 0700); file_put_contents($root . '/config.json', json_encode(['state_directory'=>$root]));
$supervisor = getenv('WORKER_SUPERVISOR') ?: '/opt/worker/supervise.php';
function waitFor(callable $ok): void {
    $start = microtime(true);
    while (true) {
        clearstatcache();
        if ($ok()) { return; }
        if (microtime(true) - $start > 10) { throw new RuntimeException('test_fixture_did_not_progress'); }
        usleep(10000);
    }
}
function startProbe(string $root, string $supervisor, string $source): array {
    file_put_contents($root . '/probe.php', "<?php\n" . $source);
    $env = getenv(); $env['WORKER_BOOTSTRAP'] = $root . '/probe.php'; $env['PROBE_ROOT'] = $root;
    $pipes=[];$p=proc_open([PHP_BINARY, $supervisor, $root . '/config.json'], [0=>['file','/dev/null','r'],1=>['file',$root.'/stdout','a'],2=>['file',$root.'/stderr','a']], $pipes, null, $env);
    if (!is_resource($p)) { throw new RuntimeException('probe_spawn_failed'); }
    return [$p,$pipes];
}
function finishProbe($p): void {
    proc_terminate($p, SIGTERM);
    if (proc_close($p) !== 0) { throw new RuntimeException('supervisor_did_not_stop_cleanly'); }
}
try {
    [$p] = startProbe($root, $supervisor, '$r=getenv("PROBE_ROOT");file_put_contents($r."/started","1");usleep(300000);file_put_contents($r."/trace","done\\n",FILE_APPEND);$busy=true;');
    waitFor(fn()=>is_file($root.'/started')); finishProbe($p);
    if (file_get_contents($root.'/trace') !== "done\n") { throw new RuntimeException('stop_lost_or_duplicated_current_work'); }
    echo "ok: stop drains the current tick and prevents a second claim\n";
    unlink($root.'/started');unlink($root.'/trace');
    [$p] = startProbe($root, $supervisor, '$r=getenv("PROBE_ROOT");$n=is_file($r."/count")?(int)file_get_contents($r."/count"):0;$busy=$n<3;if($busy)file_put_contents($r."/count",(string)($n+1));');
    $start=microtime(true);waitFor(fn()=>is_file($root.'/count') && file_get_contents($root.'/count')==='3');$elapsed=microtime(true)-$start;finishProbe($p);
    if ($elapsed > 2) { throw new RuntimeException('busy_ticks_inherit_idle_delay'); }
    echo "ok: completed work is followed immediately by the next tick\n";
    [$p] = startProbe($root, $supervisor, '$r=getenv("PROBE_ROOT");if(!is_file($r."/installed")){file_put_contents($r."/installed","1");exit(75);}file_put_contents($r."/updated","1");$busy=false;');
    waitFor(fn()=>is_file($root.'/updated'));finishProbe($p);
    echo "ok: signed runtime restart selects a new tick\n";
    [$p] = startProbe($root, $supervisor, '$r=getenv("PROBE_ROOT");file_put_contents($r."/entered","1");usleep(300000);file_put_contents($r."/maint-trace","done\\n",FILE_APPEND);$busy=true;');
    waitFor(fn()=>is_file($root.'/entered'));file_put_contents($root.'/maintenance','1');
    waitFor(fn()=>is_file($root.'/adapter-drained'));
    if(file_get_contents($root.'/maint-trace')!=="done\n")throw new RuntimeException('maintenance_did_not_drain');
    usleep(100000);if(file_get_contents($root.'/maint-trace')!=="done\n")throw new RuntimeException('maintenance_admitted_more_work');
    unlink($root.'/maintenance');waitFor(fn()=>!is_file($root.'/adapter-drained'));finishProbe($p);
    echo "ok: maintenance drains and blocks claims across restarts\n";

} finally {
    foreach (glob($root.'/*') as $file) { unlink($file); } rmdir($root);
}
