from base.cluster.machine import MachineIdentity, MachineRoleMissing, MachineRoles


def machine_identity(
    *, name: str = "test-runner", roles: MachineRoles | None = frozenset({"agent-runner"})
) -> MachineIdentity:
    def read_roles() -> MachineRoles:
        if roles is None:
            raise MachineRoleMissing("test unit is unconfigured")
        return roles

    return MachineIdentity(
        name=lambda: name,
        role=read_roles,
        host=lambda: "localhost",
        description=lambda: None,
    )


def allow_identity(*_args: object, **_kwargs: object) -> bool:
    return True


def deny_identity(*_args: object, **_kwargs: object) -> bool:
    return False


def ignore_arguments(*_args: object, **_kwargs: object) -> None:
    pass
