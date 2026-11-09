class BackupError(Exception):
    """A stable error code with a deliberately credential-free explanation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
