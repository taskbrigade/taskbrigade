"""Domain errors; the API renders them as {"error": {"code", "message"}}."""


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFound(AppError):
    status, code = 404, "not_found"


class Conflict(AppError):
    status, code = 409, "conflict"


class Unprocessable(AppError):
    status, code = 422, "unprocessable"


class Forbidden(AppError):
    status, code = 403, "forbidden"


class Gone(AppError):
    status, code = 410, "gone"
