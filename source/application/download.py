from asyncio import Semaphore, gather, sleep, to_thread
from os import environ, replace
from pathlib import Path
from shutil import copyfile
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from aiofiles import open
from curl_cffi.requests.exceptions import RequestException

from ..expansion import CacheError

# from ..module import WARNING
from ..module import (
    ERROR,
    FILE_SIGNATURES,
    FILE_SIGNATURES_LENGTH,
    MAX_WORKERS,
    logging,
    # sleep_time,
)
from ..module import retry as re_download
from ..translation import _
from .live_photo import inspect_live_pair, write_live_jpeg, write_live_mov

if TYPE_CHECKING:
    from curl_cffi.requests import AsyncSession

    from ..module import Manager

__all__ = ["Download"]


class LivePhotoTempLimitError(OSError):
    """Raised before a Live Photo chunk would exceed the RAM guard."""


class Download:
    SEMAPHORE = Semaphore(MAX_WORKERS)
    # Live Photo conversion temporarily keeps the source pair and generated
    # pair in RAM.  Serialize pairs to keep the configured tmpfs bound useful.
    LIVE_PHOTO_SEMAPHORE = Semaphore(1)
    LIVE_PHOTO_TMP_ENV = "XHS_LIVE_PHOTO_TMP"
    LIVE_PHOTO_TMP_MAX_ENV = "XHS_LIVE_PHOTO_TMP_MAX_BYTES"
    DEFAULT_LIVE_PHOTO_TMP_MAX_BYTES = 128 * 1024 * 1024
    WRITE_BUFFER_SIZE = 1024 * 1024 * 100
    LIVE_PHOTO_WRITE_BUFFER_SIZE = 1024 * 1024 * 4
    CONTENT_TYPE_MAP = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/webp": "webp",
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "audio/mp4": "m4a",
        "audio/mpeg": "mp3",
    }

    def __init__(
        self,
        manager: "Manager",
    ):
        self.manager = manager
        self.print = manager.print
        self.folder = manager.folder
        self.temp = manager.temp
        self.chunk = manager.chunk
        self.client: "AsyncSession" = manager.download_client
        self.headers = manager.blank_headers
        self.retry = manager.retry
        self.folder_mode = manager.folder_mode
        self.video_format = "mp4"
        self.live_format = "mp4"
        self.image_format = manager.image_format
        self.image_format_list = (
            "jpeg",
            "png",
            "webp",
            "avif",
            "heic",
        )
        self.image_download = manager.image_download
        self.video_download = manager.video_download
        self.video_cover_download = manager.video_cover_download
        self.live_download = manager.live_download
        self.author_archive = manager.author_archive
        self.write_mtime = manager.write_mtime
        live_photo_temp = environ.get(self.LIVE_PHOTO_TMP_ENV, "").strip()
        self.live_photo_temp = Path(
            live_photo_temp or str(self.temp.joinpath("LivePhoto"))
        )
        try:
            self.live_photo_temp_max_bytes = max(
                1,
                int(
                    environ.get(
                        self.LIVE_PHOTO_TMP_MAX_ENV,
                        self.DEFAULT_LIVE_PHOTO_TMP_MAX_BYTES,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.live_photo_temp_max_bytes = self.DEFAULT_LIVE_PHOTO_TMP_MAX_BYTES

    async def run(
        self,
        urls: list,
        lives: list,
        index: list | tuple | None,
        path: Path,
        filename: str,
        type_: str,
        mtime: int,
        cover: str | None = None,
        progress: Callable[[dict], None] | None = None,
        task_id: str | None = None,
    ) -> list[Any]:
        if type_ == _("视频"):
            tasks = self.__ready_download_video(
                urls,
                path,
                filename,
            )
            tasks += self.__ready_download_cover(
                cover,
                path,
                filename,
            )
            return await gather(
                *(
                    self.__download(
                        url,
                        path,
                        name,
                        format_,
                        mtime,
                        progress,
                        task_id,
                    )
                    for url, name, format_ in tasks
                )
            )
        elif type_ in {
            _("图文"),
            _("图集"),
        }:
            tasks = self.__ready_download_image(
                urls,
                lives,
                index,
                path,
                filename,
            )
        else:
            raise ValueError
        return await gather(
            *(
                self.__download_live_photo(
                    task[1],
                    task[2],
                    path,
                    task[3],
                    mtime,
                    progress,
                    task_id,
                )
                if task[0] == "live"
                else self.__download(
                    task[0],
                    path,
                    task[1],
                    task[2],
                    mtime,
                    progress,
                    task_id,
                )
                for task in tasks
            )
        )

    def generate_path(self, nickname: str, filename: str):
        if self.author_archive:
            folder = self.folder.joinpath(nickname)
            folder.mkdir(exist_ok=True)
        else:
            folder = self.folder
        path = self.manager.archive(folder, filename, self.folder_mode)
        path.mkdir(exist_ok=True)
        return path

    def __ready_download_video(
        self,
        urls: list[str],
        path: Path,
        name: str,
    ) -> list:
        if not self.video_download:
            logging(self.print, _("视频作品下载功能已关闭，跳过下载"))
            return []
        if self.__check_exists_path(
            path,
            f"{name}.{self.video_format}",
        ):
            return []
        return [(urls[0], name, self.video_format)]

    def __ready_download_cover(
        self,
        url: str | None,
        path: Path,
        name: str,
    ) -> list:
        if not self.video_cover_download or not url:
            return []
        if not any(
            self.__check_exists_path(
                path,
                f"{name}.{s}",
            )
            for s in self.image_format_list
        ):
            return [(url, name, self.image_format)]
        return []

    def __ready_download_image(
        self,
        urls: list[str],
        lives: list[str],
        index: list | tuple | None,
        path: Path,
        name: str,
    ) -> list[tuple]:
        tasks = []
        if not self.image_download:
            logging(self.print, _("图文作品下载功能已关闭，跳过下载"))
            return tasks
        for i, j in enumerate(zip(urls, lives), start=1):
            if index and i not in index:
                continue
            file = f"{name}_{i}"
            if self.live_download and j[1]:
                if self.__live_pair_exists(path, file):
                    continue
                tasks.append(("live", j[0], j[1], file))
                continue
            if not any(
                self.__check_exists_path(
                    path,
                    f"{file}.{s}",
                )
                for s in self.image_format_list
            ):
                tasks.append([j[0], file, self.image_format])
        return tasks

    def __live_pair_exists(self, path: Path, name: str) -> bool:
        image = path.joinpath(f"{name}.JPG")
        movie = path.joinpath(f"{name}.MOV")
        if image.exists() and movie.exists():
            logging(self.print, _("{0} 实况照片已存在，跳过下载").format(name))
            return True
        return False

    async def __download_live_photo(
        self,
        image_url: str,
        live_url: str,
        path: Path,
        name: str,
        mtime: int,
        progress: Callable[[dict], None] | None = None,
        task_id: str | None = None,
    ) -> bool:
        try:
            async with self.LIVE_PHOTO_SEMAPHORE:
                stage = self.live_photo_temp
                stage.mkdir(parents=True, exist_ok=True)
                if self.__live_photo_temp_usage(stage) >= self.live_photo_temp_max_bytes:
                    logging(
                        self.print,
                        _("实况照片 RAM 临时空间已达到上限，跳过本次转换"),
                        ERROR,
                    )
                    return False
                cover_prefix = f"{name}.__live_cover"
                motion_prefix = f"{name}.__live_motion"
                cover = self.__find_stage_file(stage, cover_prefix)
                motion = self.__find_stage_file(stage, motion_prefix)
                if not isinstance(cover, Path):
                    cover = await self.__download(
                        image_url,
                        stage,
                        cover_prefix,
                        "jpeg",
                        mtime,
                        progress,
                        task_id,
                        temp_root=stage,
                        max_temp_bytes=self.live_photo_temp_max_bytes,
                    )
                if not isinstance(motion, Path):
                    motion = await self.__download(
                        live_url,
                        stage,
                        motion_prefix,
                        self.live_format,
                        mtime,
                        progress,
                        task_id,
                        temp_root=stage,
                        max_temp_bytes=self.live_photo_temp_max_bytes,
                    )
                if not isinstance(cover, Path) or not isinstance(motion, Path):
                    return False
                if self.__live_photo_temp_usage(stage) > self.live_photo_temp_max_bytes:
                    logging(
                        self.print,
                        _("实况照片 RAM 临时空间超过上限，取消本次转换"),
                        ERROR,
                    )
                    return False
                await to_thread(
                    self.__convert_live_photo,
                    cover,
                    motion,
                    path,
                    name,
                )
            for source in (cover, motion):
                self.manager.delete(source)
            for output in (path / f"{name}.JPG", path / f"{name}.MOV"):
                if self.write_mtime and mtime:
                    self.manager.update_mtime(output, mtime)
            if progress:
                progress(
                    {
                        "task_id": task_id,
                        "filename": f"{name}.JPG/.MOV",
                        "completed_bytes": None,
                        "total_bytes": None,
                        "state": "completed",
                    }
                )
            logging(self.print, _("文件 {0}.JPG/.MOV 下载成功").format(name))
            return True
        except Exception as error:
            if progress:
                progress(
                    {
                        "task_id": task_id,
                        "filename": f"{name}.JPG/.MOV",
                        "completed_bytes": None,
                        "total_bytes": None,
                        "state": "failed",
                    }
                )
            logging(
                self.print,
                _("文件 {0} 实况照片转换失败，错误信息: {1}").format(name, repr(error)),
                ERROR,
            )
            return False
        finally:
            # Do not retain source or partial files in RAM after a failed pair.
            self.__cleanup_live_photo_stage(
                self.live_photo_temp,
                (f"{name}.__live_cover", f"{name}.__live_motion"),
            )

    @staticmethod
    def __live_photo_temp_usage(stage: Path) -> int:
        try:
            return sum(item.stat().st_size for item in stage.iterdir() if item.is_file())
        except OSError:
            return 0

    @staticmethod
    def __cleanup_live_photo_stage(stage: Path, prefixes: tuple[str, ...]) -> None:
        if not stage.is_dir():
            return
        for prefix in prefixes:
            for item in stage.glob(f"{prefix}.*"):
                try:
                    item.unlink()
                except OSError:
                    pass

    @staticmethod
    def __find_stage_file(stage: Path, prefix: str) -> Path | None:
        return next(
            (
                item
                for item in stage.glob(f"{prefix}.*")
                if not item.name.endswith(".part")
            ),
            None,
        )

    @staticmethod
    def __convert_live_photo(
        cover: Path,
        motion: Path,
        path: Path,
        name: str,
    ) -> None:
        asset_id = str(uuid4()).upper()
        # Keep both source and conversion output in the RAM staging directory.
        # The final copy is written to a .part file in the destination mount,
        # then renamed there so os.replace never crosses mount boundaries.
        cover_output = cover.parent.joinpath(f"{name}.JPG.part")
        motion_output = cover.parent.joinpath(f"{name}.MOV.part")
        destination_cover = path.joinpath(f"{name}.JPG.part")
        destination_motion = path.joinpath(f"{name}.MOV.part")
        try:
            write_live_jpeg(cover, cover_output, asset_id)
            write_live_mov(motion, motion_output, asset_id)
            inspect_live_pair(cover_output, motion_output)
            copyfile(cover_output, destination_cover)
            copyfile(motion_output, destination_motion)
            replace(destination_cover, path.joinpath(f"{name}.JPG"))
            replace(destination_motion, path.joinpath(f"{name}.MOV"))
        finally:
            for output in (
                cover_output,
                motion_output,
                destination_cover,
                destination_motion,
            ):
                if output.exists():
                    output.unlink()

    def __check_exists_glob(
        self,
        path: Path,
        name: str,
    ) -> bool:
        if any(path.glob(name)):
            logging(self.print, _("{0} 文件已存在，跳过下载").format(name))
            return True
        return False

    def __check_exists_path(
        self,
        path: Path,
        name: str,
    ) -> bool:
        if path.joinpath(name).exists():
            logging(self.print, _("{0} 文件已存在，跳过下载").format(name))
            return True
        return False

    @re_download
    async def __download(
        self,
        url: str,
        path: Path,
        name: str,
        format_: str,
        mtime: int,
        progress: Callable[[dict], None] | None = None,
        task_id: str | None = None,
        temp_root: Path | None = None,
        max_temp_bytes: int | None = None,
    ):
        async with self.SEMAPHORE:
            headers = self.headers.copy()
            temp = (temp_root or self.temp).joinpath(
                f"{name}.{format_}.part" if temp_root else f"{name}.{format_}"
            )
            completed = self.__update_headers_range(
                headers,
                temp,
            )

            def report(state: str, total: int | None = None) -> None:
                if progress:
                    progress(
                        {
                            "task_id": task_id,
                            "filename": f"{name}.{format_}",
                            "completed_bytes": completed,
                            "total_bytes": total,
                            "state": state,
                        }
                    )

            try:
                async with self.client.stream(
                    "GET",
                    url,
                    headers=headers,
                ) as response:
                    # await sleep_time()
                    if response.status_code == 416:
                        raise CacheError(
                            _("文件 {0} 缓存异常，重新下载").format(temp.name),
                        )
                    response.raise_for_status()
                    # self.__create_progress(
                    #     bar,
                    #     int(
                    #         response.headers.get(
                    #             'content-length', 0)) or None,
                    # )
                    content_length = int(response.headers.get("content-length", 0) or 0)
                    total = completed + content_length if content_length else None
                    report("downloading", total)
                    buffer = bytearray()
                    write_buffer_size = (
                        self.LIVE_PHOTO_WRITE_BUFFER_SIZE
                        if max_temp_bytes is not None
                        else self.WRITE_BUFFER_SIZE
                    )
                    async with open(temp, "ab") as f:
                        async for chunk in response.aiter_content(self.chunk):
                            if (
                                max_temp_bytes is not None
                                and self.__live_photo_temp_usage(temp.parent)
                                + len(buffer)
                                + len(chunk)
                                > max_temp_bytes
                            ):
                                raise LivePhotoTempLimitError(
                                    "Live Photo RAM 临时空间达到上限"
                                )
                            buffer.extend(chunk)
                            if len(buffer) >= write_buffer_size:
                                await f.write(bytes(buffer))
                                buffer.clear()
                            completed += len(chunk)
                            report("downloading", total)
                        if buffer:
                            await f.write(bytes(buffer))
                real = await self.__suffix_with_file(
                    temp,
                    path,
                    name,
                    # suffix,
                    format_,
                )
                self.manager.move(
                    temp,
                    real,
                    mtime,
                    self.write_mtime,
                )
                report("completed", total)
                logging(self.print, _("文件 {0} 下载成功").format(real.name))
                return real
            except RequestException as error:
                report("failed")
                logging(
                    self.print,
                    _("网络异常，{0} 下载失败，错误信息: {1}").format(
                        name, repr(error)
                    ),
                    ERROR,
                )
                return False
            except CacheError as error:
                report("failed")
                self.manager.delete(temp)
                logging(
                    self.print,
                    str(error),
                    ERROR,
                )
                return False
            except OSError as error:
                report("failed")
                self.manager.delete(temp)
                logging(
                    self.print,
                    _("文件 {0} 写入临时空间失败，错误信息: {1}").format(
                        name, repr(error)
                    ),
                    ERROR,
                )
                return False

    @staticmethod
    def __create_progress(
        bar,
        total: int | None,
        completed=0,
    ):
        if bar:
            bar.update(total=total, completed=completed)

    @staticmethod
    def __update_progress(bar, advance: int):
        if bar:
            bar.advance(advance)

    @classmethod
    def __extract_type(cls, content: str) -> str:
        return cls.CONTENT_TYPE_MAP.get(content, "")

    async def __head_file(
        self,
        url: str,
        headers: dict[str, str],
        suffix: str,
    ) -> tuple[int, str]:
        """未使用"""
        response = await self.client.head(
            url,
            headers=headers,
        )
        # await sleep_time()
        response.raise_for_status()
        suffix = self.__extract_type(response.headers.get("Content-Type")) or suffix
        length = response.headers.get("Content-Length", 0)
        return int(length), suffix

    @staticmethod
    def __get_resume_byte_position(file: Path) -> int:
        return file.stat().st_size if file.is_file() else 0

    def __update_headers_range(
        self,
        headers: dict[str, str],
        file: Path,
    ) -> int:
        headers["Range"] = f"bytes={(p := self.__get_resume_byte_position(file))}-"
        return p

    async def __suffix_with_file(
        self,
        temp: Path,
        path: Path,
        name: str,
        default_suffix: str,
    ) -> Path:
        try:
            async with open(temp, "rb") as f:
                file_start = await f.read(FILE_SIGNATURES_LENGTH)
            for offset, signature, suffix in FILE_SIGNATURES:
                if file_start[offset : offset + len(signature)] == signature:
                    return path.joinpath(f"{name}.{suffix}")
        except Exception as error:
            logging(
                self.print,
                _("文件 {0} 格式判断失败，错误信息：{1}").format(
                    temp.name, repr(error)
                ),
                ERROR,
            )
        return path.joinpath(f"{name}.{default_suffix}")
