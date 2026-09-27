from asyncio import CancelledError
from contextlib import suppress
from shutil import move
from typing import TYPE_CHECKING

from aiosqlite import connect

if TYPE_CHECKING:
    from ..module import Manager

__all__ = ["IDRecorder", "DataRecorder", "MapRecorder", "RECORD_COLUMNS_EXPECTED"]

# 下载记录扩展字段（单一来源：recorder 与 WebUI 共用，避免两侧定义漂移）
RECORD_COLUMNS_EXPECTED = ("作者昵称", "作者ID", "作品链接", "作品标题", "下载时间")


class IDRecorder:
    # 下载记录扩展字段（WebUI 展示作者昵称/作者ID/作品链接/作品标题/下载时间）
    RECORD_COLUMNS = RECORD_COLUMNS_EXPECTED

    def __init__(self, manager: "Manager"):
        self.name = "ExploreID.db"
        self.file = manager.root.joinpath(self.name)
        self.changed = False
        self.switch = manager.download_record
        self.database = None
        self.cursor = None

    async def _connect_database(self):
        self.database = await connect(self.file)
        self.cursor = await self.database.cursor()
        await self.database.execute(
            "CREATE TABLE IF NOT EXISTS explore_id ("
            "ID TEXT PRIMARY KEY,"
            "作者昵称 TEXT,"
            "作者ID TEXT,"
            "作品链接 TEXT,"
            "作品标题 TEXT,"
            "下载时间 TEXT"
            ");"
        )
        # 旧库平滑迁移：检测缺失列并 ALTER TABLE 补充，不清空、不重建已有记录
        cursor = await self.database.execute("PRAGMA table_info(explore_id)")
        existing = {row[1] for row in await cursor.fetchall()}
        for column in self.RECORD_COLUMNS:
            if column not in existing:
                await self.database.execute(
                    f"ALTER TABLE explore_id ADD COLUMN {column} TEXT"
                )
        await self.database.commit()

    async def select(self, id_: str):
        if self.switch:
            await self.cursor.execute("SELECT ID FROM explore_id WHERE ID=?", (id_,))
            return await self.cursor.fetchone()

    async def add(
        self,
        id_: str,
        name: str = None,
        *args,
        **kwargs,
    ) -> None:
        if self.switch:
            # 显式列名写入：兼容旧库单列结构（迁移后自动补列）与完整作品信息
            await self.database.execute(
                "REPLACE INTO explore_id "
                "(ID, 作者昵称, 作者ID, 作品链接, 作品标题, 下载时间) "
                "VALUES (?,?,?,?,?,?)",
                (
                    id_,
                    kwargs.get("作者昵称"),
                    kwargs.get("作者ID"),
                    kwargs.get("作品链接"),
                    kwargs.get("作品标题"),
                    kwargs.get("下载时间"),
                ),
            )
            await self.database.commit()

    async def __delete(self, id_: str) -> None:
        if id_:
            await self.database.execute("DELETE FROM explore_id WHERE ID=?", (id_,))
            await self.database.commit()

    async def delete(self, ids: list[str]):
        if self.switch:
            [await self.__delete(i) for i in ids]

    async def all(self):
        if self.switch:
            await self.cursor.execute("SELECT ID FROM explore_id")
            return [i[0] for i in await self.cursor.fetchall()]

    async def count(self, query: str = "") -> int:
        if not self.switch:
            return 0
        statement = "SELECT COUNT(*) FROM explore_id"
        parameters = ()
        if query:
            statement += " WHERE INSTR(LOWER(ID), LOWER(?)) > 0"
            parameters = (query,)
        async with self.database.execute(statement, parameters) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def page(
        self,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        if not self.switch:
            return []
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        statement = "SELECT ID FROM explore_id"
        parameters: tuple = ()
        if query:
            statement += " WHERE INSTR(LOWER(ID), LOWER(?)) > 0"
            parameters = (query,)
        statement += " ORDER BY rowid DESC LIMIT ? OFFSET ?"
        async with self.database.execute(
            statement,
            (*parameters, limit, offset),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def clear(self) -> int:
        if not self.switch:
            return 0
        count = await self.count()
        await self.database.execute("DELETE FROM explore_id")
        await self.database.commit()
        return count

    async def __aenter__(self):
        self.compatible()
        await self._connect_database()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        with suppress(CancelledError):
            await self.cursor.close()
        await self.database.close()

    def compatible(
        self,
    ):
        if (
            not self.changed
            and (old := self.file.parent.parent.joinpath(self.name)).exists()
            and not self.file.exists()
        ):
            move(old, self.file)


class DataRecorder(IDRecorder):
    DATA_TABLE = (
        ("采集时间", "TEXT"),
        ("作品ID", "TEXT PRIMARY KEY"),
        ("作品类型", "TEXT"),
        ("作品标题", "TEXT"),
        ("作品描述", "TEXT"),
        ("作品标签", "TEXT"),
        ("发布时间", "TEXT"),
        ("最后更新时间", "TEXT"),
        ("收藏数量", "TEXT"),
        ("评论数量", "TEXT"),
        ("分享数量", "TEXT"),
        ("点赞数量", "TEXT"),
        ("作者昵称", "TEXT"),
        ("作者ID", "TEXT"),
        ("作者链接", "TEXT"),
        ("作品链接", "TEXT"),
        ("下载地址", "TEXT"),
        ("动图地址", "TEXT"),
    )

    def __init__(self, manager: "Manager"):
        super().__init__(manager)
        self.name = "ExploreData.db"
        self.file = manager.folder.joinpath(self.name)
        self.changed = True
        self.switch = manager.record_data

    async def _connect_database(self):
        self.database = await connect(self.file)
        self.cursor = await self.database.cursor()
        await self.database.execute(f"""CREATE TABLE IF NOT EXISTS explore_data (
        {",".join(" ".join(i) for i in self.DATA_TABLE)}
        );""")
        await self.database.commit()

    async def select(self, id_: str):
        pass

    async def add(self, **kwargs) -> None:
        if self.switch:
            await self.database.execute(
                f"""REPLACE INTO explore_data (
        {", ".join(i[0] for i in self.DATA_TABLE)}
        ) VALUES (
        {", ".join("?" for _ in kwargs)}
        );""",
                self.__generate_values(kwargs),
            )
            await self.database.commit()

    async def __delete(self, id_: str) -> None:
        pass

    async def delete(self, ids: list | tuple):
        pass

    async def all(self):
        pass

    def __generate_values(self, data: dict) -> tuple:
        return tuple(data[i] for i, _ in self.DATA_TABLE)


class MapRecorder(IDRecorder):
    def __init__(self, manager: "Manager"):
        super().__init__(manager)
        self.name = "MappingData.db"
        self.file = manager.root.joinpath(self.name)
        self.switch = manager.author_archive

    async def _connect_database(self):
        self.database = await connect(self.file)
        self.cursor = await self.database.cursor()
        await self.database.execute(
            "CREATE TABLE IF NOT EXISTS mapping_data ("
            "ID TEXT PRIMARY KEY,"
            "NAME TEXT NOT NULL"
            ");"
        )
        await self.database.commit()

    async def select(self, id_: str):
        if self.switch:
            await self.cursor.execute(
                "SELECT NAME FROM mapping_data WHERE ID=?", (id_,)
            )
            return await self.cursor.fetchone()

    async def add(self, id_: str, name: str, *args, **kwargs) -> None:
        if self.switch:
            await self.database.execute(
                "REPLACE INTO mapping_data VALUES (?, ?);",
                (
                    id_,
                    name,
                ),
            )
            await self.database.commit()

    async def __delete(self, id_: str) -> None:
        pass

    async def delete(self, ids: list[str]):
        pass

    async def all(self):
        if self.switch:
            await self.cursor.execute("SELECT ID, NAME FROM mapping_data")
            return await self.cursor.fetchall()
