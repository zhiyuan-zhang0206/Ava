from base.packages.plugins.extensions import ExtensionRegistry
from meiva.sdk_surface import install
from meiva.sdk_surface.installation import installed

install.install(ExtensionRegistry(()))
installation = installed()
assert installation is not None
install.uninstall()
installation.sampling.error = (
    ValueError("a removed surface must not close at interpreter exit"),
    None,
)
print("uninstalled")
