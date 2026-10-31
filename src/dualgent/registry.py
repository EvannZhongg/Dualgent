from urllib.parse import urlsplit

from .contracts import Driver, InteractionError, Surface


class Environments:
    def __init__(self, drivers: dict[str, Driver]):
        self.drivers = drivers
        self.errors: list[str] = []

    def driver(self, surface: Surface) -> Driver:
        return self.drivers[surface.kind]

    async def discover(self) -> list[Surface]:
        self.errors = []
        result = []
        for kind, driver in self.drivers.items():
            try:
                result.extend(await driver.discover())
            except Exception as error:
                self.errors.append(f"{kind}: {error}")
        return result

    async def refresh_bindings(self, surfaces: dict[str, Surface]) -> dict[str, Surface]:
        """Explicit user continuation may refresh metadata, never window/tab identity or origin."""
        current = {s.id: s for s in await self.discover()}
        refreshed = {}
        for surface_id, bound in surfaces.items():
            actual = current.get(surface_id)
            if actual is None or actual.kind != bound.kind:
                raise InteractionError("任务绑定的环境已关闭或重启；请重新选择环境创建任务。")
            if bound.kind == "web":
                before, after = urlsplit(bound.url), urlsplit(actual.url)
                if (before.scheme, before.netloc) != (after.scheme, after.netloc):
                    raise InteractionError("页面已离开任务授权的站点；请重新选择环境创建任务。")
            refreshed[surface_id] = actual
        return refreshed
