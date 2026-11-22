"""Add safe manual scheduling and observable renewal results to the pinned engine.

Installed into SessionController by adapt_upstream; all automatic and manual
attempts still use its original issuer, validation, lock and atomic session save.
"""
import time


def install(controller):
    original_status = controller.status
    original_renew = controller.renew_once

    def metrics(self):
        if not hasattr(self, '_manager_renewal'):
            self._manager_renewal = {
                'running': False, 'last_attempt_at': None, 'last_success_at': None,
                'last_success_generation': None, 'last_error': None,
            }
        return self._manager_renewal

    def status(self):
        return {**original_status(self), 'renewal': {**metrics(self), 'queued': self._force_renewal}}

    async def renew_once(self):
        state = metrics(self)
        state.update(running=True, last_attempt_at=time.time(), last_error=None)
        self.on_change()
        try:
            result = await original_renew(self)
            state.update(last_success_at=time.time(),
                         last_success_generation=self.session.status()['generation'])
            return result
        except Exception as error:
            # Only fixed engine error codes leave the worker, never exception text.
            state['last_error'] = getattr(error, 'code', 'HELPER_FAILED')
            raise
        finally:
            state['running'] = False
            self.on_change()

    def request_manual_renewal(self):
        from src.auth.session_bundle import SessionError
        if self.session.seed() is None:
            raise SessionError('SDK_SEED')
        if self._stopping:
            raise SessionError('STOPPED')
        now = time.monotonic()
        if (metrics(self)['running'] or self._force_renewal or self._lock.locked()
                or now < getattr(self, '_manager_manual_after', 0)):
            raise SessionError('BUSY')
        self._manager_manual_after = now + 60
        self.request_renewal()

    controller.status = status
    controller.renew_once = renew_once
    controller.request_manual_renewal = request_manual_renewal
