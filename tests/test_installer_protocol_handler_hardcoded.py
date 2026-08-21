from __future__ import annotations

# reclaim-notify: protocol-handler injection gate.
#
# packaging/reclaim.iss registers "reclaim-notify:" as an HKCU protocol handler with a
# hardcoded command string (no %1/%L placeholder) -- this is deliberate: a placeholder would
# let anything that can construct a reclaim-notify:...  URI pass an argument straight into the
# launched command (Windows resolves ShellExecute on this scheme without any argument
# validation of its own). See packaging/reclaim.iss's [Registry] section comment for the full
# rationale.
#
# Nothing else enforces that this stays hardcoded -- if someone later edits the .iss to add
# %1/%L/%* (e.g. "to support more toast actions"), the injection surface reopens silently with
# no test catching it until this gate existed.
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent

# Inno Setup / shell placeholder tokens that would let an external argument reach the launched
# command line. %1 and %L are the two Windows shell classically substitutes with the URI/path
# passed to the handler; %* and %2.. are included defensively in case a future edit tries a
# different substitution shape.
_PLACEHOLDER_TOKENS: tuple[str, ...] = ("%1", "%2", "%L", "%l", "%*")


def _notify_command_value() -> str:
    """Extract the ValueData of the reclaim-notify\\shell\\open\\command registry entry."""
    iss_text = (_REPO_ROOT / "packaging" / "reclaim.iss").read_text(encoding="utf-8")
    match = re.search(
        r'Subkey:\s*"Software\\Classes\\reclaim-notify\\shell\\open\\command";[^\n]*'
        r'ValueData:\s*"((?:[^"]|"")*)"',
        iss_text,
    )
    assert match is not None, (
        "packaging/reclaim.iss no longer defines the reclaim-notify\\shell\\open\\command "
        "registry entry -- this gate has nothing to check. If the handler was intentionally "
        "removed, delete this test; if it was renamed/restructured, update the regex above."
    )
    return match.group(1)


def test_notify_protocol_handler_command_is_hardcoded() -> None:
    """The reclaim-notify handler's command line must not accept an external argument.

    A %1/%L/%* placeholder here would let any process that can invoke
    "reclaim-notify:<arbitrary text>" (e.g. via ShellExecute or a browser navigation) inject
    that text into the launched reclaim.exe command line.
    """
    command = _notify_command_value()
    found = [token for token in _PLACEHOLDER_TOKENS if token in command]
    assert not found, (
        f"packaging/reclaim.iss's reclaim-notify\\shell\\open\\command now contains "
        f"placeholder token(s) {found} in {command!r} -- this reopens the URI-argument-"
        "injection gap the hardcoded command line was written to prevent. Do not add %1/%L/%* "
        "here; the command must take zero arguments from the invoking URI."
    )


def test_notify_protocol_handler_command_still_present_and_nonempty() -> None:
    """Sanity check that the extraction regex above is actually matching real content.

    Guards against the placeholder check above silently passing because the regex stopped
    matching anything (e.g. after an unrelated .iss reformat) rather than because the command
    is genuinely clean.
    """
    command = _notify_command_value()
    assert "check-disk-space" in command and "--apply-snooze" in command, (
        f"reclaim-notify command value {command!r} no longer looks like the expected "
        "check-disk-space --apply-snooze invocation -- the extraction regex may be matching "
        "the wrong thing, which would make the placeholder-token gate above meaningless."
    )
