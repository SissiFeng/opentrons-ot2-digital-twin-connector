"""Application-level instrument routing; semantic bridge contracts stay unchanged."""

from . import flex, ot2_console


def module(profile):
    schema = profile.get("schema") if isinstance(profile, dict) else None
    if schema == "ot2.profile/1":
        return ot2_console
    if schema == "flex.profile/1":
        return flex
    raise ValueError("Choose a supported OT-2 or Flex profile")


def profile_template(instrument="ot2"):
    if instrument not in ("ot2", "flex"):
        raise ValueError("Unknown instrument")
    return (ot2_console if instrument == "ot2" else flex).profile_template()


def validate_profile(profile, *, hardware=False):
    return module(profile).validate_profile(profile, hardware=hardware)


def make_plan(profile, run_id):
    return module(profile).make_plan(profile, run_id)


def validate_plan(plan):
    if not isinstance(plan, dict) or "profile" not in plan:
        raise ValueError("Plan must include an instrument profile")
    return module(plan["profile"]).validate_plan(plan)


def is_ot2(profile):
    return module(profile) is ot2_console


def native_adapter(profile, actions, runtime):
    cls = ot2_console.NativeOT2HomeAdapter if is_ot2(profile) else flex.NativeFlexAdapter
    return cls(profile, actions, runtime)
