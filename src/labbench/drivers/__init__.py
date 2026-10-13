"""Host drivers (Linux/SSH and Windows/WinRM) behind one abstraction."""

from .base import CommandResult, DriverError, HostDriver
from .factory import detect_mode, driver_for
from .linux_ssh import LinuxSSHDriver
from .windows_winrm import WindowsWinRMDriver

__all__ = [
    "CommandResult",
    "DriverError",
    "HostDriver",
    "LinuxSSHDriver",
    "WindowsWinRMDriver",
    "detect_mode",
    "driver_for",
]
