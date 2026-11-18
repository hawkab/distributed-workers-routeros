<?php
declare(strict_types=1);
// Reuse the signed SDK bootstrap, including its lock, identity, durable outbox,
// lease handling and updater. Successful work starts the next tick immediately.
require (getenv('WORKER_BOOTSTRAP') ?: '/opt/fleet/php/bootstrap.php');
exit(($busy ?? false) ? 76 : 0);
