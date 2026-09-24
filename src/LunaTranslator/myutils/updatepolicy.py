"""Binary update policy for the Git-maintained fork."""

# Official binary packages replace fork-specific Python and resource files.
# Integrate upstream changes through Git instead of the in-app updater.
ALLOW_IN_APP_BINARY_UPDATES = False


def in_app_update_enabled(profile_setting=True):
    return ALLOW_IN_APP_BINARY_UPDATES and bool(profile_setting)
