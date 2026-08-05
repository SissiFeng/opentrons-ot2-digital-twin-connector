"""Expected, operator-recoverable failures for digital-twin unit operations."""


class CalibrationNotConfirmedError(Exception):
    """The active deck calibration is unconfirmed; verify it and restart with a confirmed calibration_id."""


class ConfigurationMismatchError(Exception):
    """The configured device identity or geometry differs from hardware; correct the pinned configuration."""


class NotHomedError(Exception):
    """Required axes are not homed; run Home before retrying the operation."""


class MovementOutOfBoundsError(Exception):
    """The target is outside configured or hardware travel limits; correct the deck location or calibration."""


class PipetteNotAttachedError(Exception):
    """No matching pipette is attached; install the configured pipette and restart or update the configuration."""


class TipStateError(Exception):
    """The requested action conflicts with connector tip state; reconcile tip presence before retrying."""


class TipPickupError(Exception):
    """Tip pickup did not complete; inspect the rack and pipette, then reconcile tip presence."""


class TipDropError(Exception):
    """Tip release did not complete; inspect the pipette and trash, then reconcile tip presence."""


class LiquidVolumeOutOfRangeError(Exception):
    """The requested volume exceeds pipette or tracked-liquid limits; correct the volume and retry."""


class LiquidStateUnknownError(Exception):
    """Tracked liquid volume is unknown after interruption; reconcile or replace the tip before continuing."""


class StateReconciliationRequiredError(Exception):
    """A prior side-effecting operation has an uncertain outcome; reconcile physical state before retrying."""
