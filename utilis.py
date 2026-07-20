import re

# Strict allowlist for package specifiers: name, optional exact-version pin.
# Rejects anything starting with '-' (blocks flag injection), rejects URLs,
# VCS specs (git+...), and any shell/metacharacters. 
_LIB_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,213}(==[A-Za-z0-9_.!+-]+)?$")
#  regex pattern to catch -- flags , and commands that can download python packages via Version Control systems
_MAX_LIBS = 25  # prevents Denial Of Service attacks (since install_libs_sandbox tool runs inside a loose docker container which has 100% cpu utilization privialge)


def _validate_libraries(libraries: list) -> list[str]:
    """Validates and normalizes the library list. Raises ValueError on anything suspicious."""
    if not isinstance(libraries, list) or not libraries:
        raise ValueError("libraries must be a non-empty list of package names.")
    if len(libraries) > _MAX_LIBS:
        raise ValueError(f"Too many libraries requested (max {_MAX_LIBS}).")

    cleaned = []
    for lib in libraries:
        if not isinstance(lib, str):
            raise ValueError(f"Invalid library entry (not a string): {lib!r}")
        lib = lib.strip()
        if not _LIB_PATTERN.match(lib):
            raise ValueError(
                f"Rejected library spec '{lib}': only 'name' or 'name==version' "
                f"is allowed. No URLs, flags, or shell characters."
            )
        cleaned.append(lib)
    return cleaned


