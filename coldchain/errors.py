"""Domain errors shared by the service and web layers."""


class ValidationError(Exception):
    """Input was rejected before/while writing; nothing was persisted."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = list(details or [])


class ConflictError(Exception):
    """The request was based on a stale view of the database."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = list(details or [])


class NotFoundError(Exception):
    """The requested month or knowledge version does not exist."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message
