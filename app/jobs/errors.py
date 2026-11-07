class JobError(Exception):
    """Generic job-engine error (bad request, e.g. retrying a job that cannot be retried)."""


class PermanentJobError(Exception):
    """Raised by a handler to fail the job right away, without retries."""


class JobCancelled(Exception):
    """Raised by JobContext.checkpoint() when cancellation was requested."""


class RetryLater(Exception):
    """Raised by a handler to put the job back in the queue after `delay` seconds.

    The attempt is not consumed: the job did nothing wrong, a service it needs is unavailable.
    Other jobs in the queue keep running meanwhile.
    """

    def __init__(self, message: str, delay: float = 300.0) -> None:
        super().__init__(message)
        self.delay = max(float(delay), 1.0)
