from .state_reader import load_latest_oms_actual_state, load_latest_oms_control_feedback


def run_oms_cycle(config):
    from .runtime import run_oms_cycle as _run_oms_cycle
    return _run_oms_cycle(config)


def run_oms_validation_suite(config):
    from .validation import run_oms_validation_suite as _run_oms_validation_suite
    return _run_oms_validation_suite(config)


__all__ = [
    "load_latest_oms_actual_state",
    "load_latest_oms_control_feedback",
    "run_oms_cycle",
    "run_oms_validation_suite",
]
