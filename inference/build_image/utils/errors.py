"""Local copy of error definitions for agent_v2."""

from __future__ import annotations


class ProcessingError(Exception):
    """Base class for all processing related errors."""

    def __repr__(self) -> str:  # pragma: no cover - convenience
        return f"<{self.__class__.__name__}: {self}>"


class ImageBuildError(ProcessingError):
    def __init__(self, message: str, docker_path: str | None = None):
        super().__init__(message)
        self.docker_path = docker_path

class EvalTimeoutError(ProcessingError):
    def __init__(self, timeout: int):
        super().__init__(f"eval_script timed out after {timeout}s")
        self.timeout = timeout


class EvalNoExitCodeError(ProcessingError):
    def __init__(self, output: str):
        super().__init__("OMNIGRIL_EXIT_CODE not detected")
        self.output = output


class CommandError(ProcessingError):
    def __init__(self, failures: list[dict]):
        self.failures = failures

        details: list[str] = []
        for failure in failures:
            cmd = failure.get("cmd") or failure.get("tool")
            exit_code = failure.get("exit_code")
            if exit_code is None:
                tried = failure.get("tried") or []
                if tried:
                    first_try = tried[0] or {}
                    exit_code = first_try.get("exit_code")
                    cmd = cmd or first_try.get("cmd")
            details.append(f"{cmd}→exit={exit_code}")
        self.summary = "; ".join(details)

        friendly_message = None
        for failure in reversed(failures):
            friendly_message = (
                failure.get("user_message")
                or failure.get("message")
                or failure.get("output")
            )
            if friendly_message:
                break

        super().__init__(friendly_message or f"commands failed: {self.summary}")


class ParsingError(ProcessingError):
    def __init__(self, message: str):
        super().__init__(f"ParsingError: {message}")
