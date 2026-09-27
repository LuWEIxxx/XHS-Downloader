"""ExploreID.db 兼容迁移测试。

覆盖点：
1. 数据库不存在时创建完整扩展表
2. 旧的单列 ID 数据库自动迁移到扩展结构（ALTER TABLE 补列）
3. 已有 6 列数据库重复初始化不报错
4. 旧记录 ID 不丢失（不删、不重建）
5. 新记录可写入完整扩展字段
6. 仅传 ID 的调用也可写入
7. 不再出现 "table explore_id has 6 columns but 1 values were supplied"
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import aiosqlite

from source.module.recorder import IDRecorder, RECORD_COLUMNS_EXPECTED


class IDRecorderMigrationTests(unittest.TestCase):
    """直接针对 SQLite 文件验证表结构与迁移行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _manager(self, download_record=True):
        return SimpleNamespace(
            root=self.root,
            download_record=download_record,
        )

    async def _columns(self, db_file: Path) -> list[str]:
        async with aiosqlite.connect(db_file) as db:
            cur = await db.execute("PRAGMA table_info(explore_id)")
            return [row[1] for row in await cur.fetchall()]

    async def _rows(self, db_file: Path, sql="SELECT ID FROM explore_id"):
        async with aiosqlite.connect(db_file) as db:
            cur = await db.execute(sql)
            return await cur.fetchall()

    def test_creates_full_table_when_new(self):
        recorder = IDRecorder(self._manager())
        db_file = self.root / "ExploreID.db"
        self.assertFalse(db_file.exists())

        async def flow():
            async with recorder:
                pass

        asyncio.run(flow())
        self.assertTrue(db_file.exists())
        columns = asyncio.run(self._columns(db_file))
        self.assertEqual(["ID", *RECORD_COLUMNS_EXPECTED], columns)

    def test_migrates_legacy_single_column_db(self):
        """旧版只有 ID 字段的数据库应被补齐扩展列，且旧记录保留。"""
        db_file = self.root / "ExploreID.db"

        async def seed():
            async with aiosqlite.connect(db_file) as db:
                await db.execute("CREATE TABLE explore_id (ID TEXT PRIMARY KEY)")
                await db.execute("INSERT INTO explore_id VALUES ('legacy-1')")
                await db.execute("INSERT INTO explore_id VALUES ('legacy-2')")
                await db.commit()

        asyncio.run(seed())
        self.assertEqual(["ID"], asyncio.run(self._columns(db_file)))

        recorder = IDRecorder(self._manager())

        async def flow():
            async with recorder:
                pass

        asyncio.run(flow())
        columns = asyncio.run(self._columns(db_file))
        self.assertEqual(["ID", *RECORD_COLUMNS_EXPECTED], columns)
        rows = asyncio.run(self._rows(db_file))
        self.assertEqual(2, len(rows))
        self.assertIn(("legacy-1",), rows)
        self.assertIn(("legacy-2",), rows)

    def test_reinit_existing_full_table_is_idempotent(self):
        """已有 6 列的表重复初始化不应报错。"""
        recorder = IDRecorder(self._manager())
        db_file = self.root / "ExploreID.db"

        async def flow():
            async with recorder:
                pass
            async with IDRecorder(self._manager()):
                pass
            async with IDRecorder(self._manager()):
                pass

        asyncio.run(flow())
        self.assertEqual(["ID", *RECORD_COLUMNS_EXPECTED], asyncio.run(self._columns(db_file)))

    def test_add_full_record_writes_all_columns(self):
        """写入完整作品信息时五个扩展字段都应落库。"""
        recorder = IDRecorder(self._manager())
        db_file = self.root / "ExploreID.db"

        async def flow():
            async with recorder:
                await recorder.add(
                    "full-1",
                    作者昵称="测试作者",
                    作者ID="user-1",
                    作品链接="https://www.xiaohongshu.com/explore/full-1",
                    作品标题="测试标题",
                    下载时间="2026-09-27 15:00:00",
                )

        asyncio.run(flow())
        rows = asyncio.run(
            self._rows(
                db_file,
                "SELECT ID, 作者昵称, 作者ID, 作品链接, 作品标题, 下载时间 FROM explore_id",
            )
        )
        self.assertEqual(
            (
                "full-1",
                "测试作者",
                "user-1",
                "https://www.xiaohongshu.com/explore/full-1",
                "测试标题",
                "2026-09-27 15:00:00",
            ),
            rows[0],
        )

    def test_add_id_only_still_works(self):
        """仅传 ID 的旧式调用仍可写入，其余字段为空。"""
        recorder = IDRecorder(self._manager())
        db_file = self.root / "ExploreID.db"

        async def flow():
            async with recorder:
                await recorder.add("solo-1")

        asyncio.run(flow())
        rows = asyncio.run(
            self._rows(db_file, "SELECT ID, 作者昵称, 下载时间 FROM explore_id")
        )
        self.assertEqual(("solo-1", None, None), rows[0])

    def test_add_on_legacy_db_does_not_raise_column_mismatch(self):
        """旧单列库迁移后写入不得抛 'has 6 columns but 1 values'。"""
        db_file = self.root / "ExploreID.db"

        async def seed():
            async with aiosqlite.connect(db_file) as db:
                await db.execute("CREATE TABLE explore_id (ID TEXT PRIMARY KEY)")
                await db.execute("INSERT INTO explore_id VALUES ('old-1')")
                await db.commit()

        asyncio.run(seed())
        recorder = IDRecorder(self._manager())

        async def flow():
            async with recorder:
                await recorder.add("new-1", 作者昵称="新作者")
                await recorder.add("new-2")

        asyncio.run(flow())  # 不抛异常即通过
        rows = asyncio.run(self._rows(db_file))
        ids = {r[0] for r in rows}
        self.assertEqual({"old-1", "new-1", "new-2"}, ids)

    def test_select_and_all_after_migration(self):
        """迁移后 select / all 查询仍正常。"""
        db_file = self.root / "ExploreID.db"

        async def seed():
            async with aiosqlite.connect(db_file) as db:
                await db.execute("CREATE TABLE explore_id (ID TEXT PRIMARY KEY)")
                await db.execute("INSERT INTO explore_id VALUES ('x-1')")
                await db.commit()

        asyncio.run(seed())
        recorder = IDRecorder(self._manager())

        async def flow():
            async with recorder:
                found = await recorder.select("x-1")
                missing = await recorder.select("nope")
                every = await recorder.all()
                return found, missing, every

        found, missing, every = asyncio.run(flow())
        self.assertEqual(("x-1",), found)
        self.assertIsNone(missing)
        self.assertIn("x-1", every)

    def test_switch_off_disables_write(self):
        """download_record 关闭时不写入。"""
        recorder = IDRecorder(self._manager(download_record=False))
        db_file = self.root / "ExploreID.db"

        async def flow():
            async with recorder:
                await recorder.add("no-1", 作者昵称="作者")

        asyncio.run(flow())
        rows = asyncio.run(self._rows(db_file))
        self.assertEqual([], rows)


class WebUIExploreIdReadTests(unittest.TestCase):
    """验证 WebUI 的 _migrate_explore_id 能读取扩展列并展示。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_file = Path(self.tmp.name) / "ExploreID.db"

    def test_webui_migration_adds_columns_and_reads(self):
        import webui

        async def seed():
            async with aiosqlite.connect(self.db_file) as db:
                await db.execute("CREATE TABLE explore_id (ID TEXT PRIMARY KEY)")
                await db.execute("INSERT INTO explore_id VALUES ('w-1')")
                await db.commit()

        asyncio.run(seed())

        async def flow():
            async with aiosqlite.connect(self.db_file) as db:
                await webui._migrate_explore_id(db)
                async with db.execute(
                    "SELECT ID, 作者昵称, 作者ID, 作品链接, 作品标题, 下载时间 "
                    "FROM explore_id ORDER BY rowid DESC"
                ) as cur:
                    return await cur.fetchall()

        rows = asyncio.run(flow())
        self.assertEqual(1, len(rows))
        self.assertEqual("w-1", rows[0][0])
        # 扩展列存在且为 NULL（不是查询失败）
        self.assertEqual([None] * 5, list(rows[0][1:]))

    def test_webui_columns_match_recorder_columns(self):
        import webui

        self.assertEqual(
            tuple(webui.EXPLORE_ID_COLUMNS),
            tuple(RECORD_COLUMNS_EXPECTED),
        )


if __name__ == "__main__":
    unittest.main()
