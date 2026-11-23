import logging
import time
from pathlib import Path

from src.services import procurement_execution
from src.services.execution import claim, execute, materialize_due
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def cycle():
    """先处理到期的定时运行与差分运行，再领取取档方案。"""
    materialize_due()
    lease = claim()
    heartbeat.touch()
    if lease:
        execute(lease)
        heartbeat.touch()
        return True
    plan_lease = procurement_execution.claim()
    if plan_lease:
        procurement_execution.execute(plan_lease)
        heartbeat.touch()
        return True
    return False


def main():
    while True:
        try:
            if not cycle():
                wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
