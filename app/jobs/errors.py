class JobError(Exception):
    """Generic job-engine error (bad request, e.g. retrying a job that cannot be retried)."""


class PermanentJobError(Exception):
    """Raised by a handler to fail the job right away, without retries."""


class JobCancelled(Exception):
    """Raised by JobContext.checkpoint() when cancellation was requested."""
