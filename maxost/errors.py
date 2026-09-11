class BridgeError(Exception):
    """Only this class's messages are safe to display to an end user."""


class RetryLater(BridgeError):
    def __init__(self, message: str = "Соединение временно недоступно", delay: float = 5):
        super().__init__(message)
        self.delay = delay


class Uncertain(BridgeError):
    """A side effect may have happened. Never retry automatically."""


class Rejected(BridgeError):
    """A definite non-retryable rejection or unsupported input."""
