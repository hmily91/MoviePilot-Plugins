"""
订阅自动保种助手 - 证据链收集与分级清理（宿主交互层）。

本模块是插件中唯一接触 MoviePilot 宿主业务数据的地方，严格遵守 V3 边界：
    - 宿主订阅只读走 ``SubscribeOper``，删除走 ``SubscribeOper.delete``；
    - 历史记录走 ``TransferHistoryOper`` / ``DownloadHistoryOper``；
    - 下载器种子走稳定门面 ``app.chain.download.DownloadChain``；
    - 不导入、不操作任何 ``app.db.models.*`` 宿主 ORM Model；
    - 不自建事务、不在插件单例里复用 Session 或 Oper 实例。

所有宿主调用均为「延迟导入 + 独立 try/except + getattr 兜底」，某个能力在
不同 V3 小版本间缺失时只记录告警，不会让插件崩溃或中断后续清理步骤。

安全约束（硬性）：
    - 只删除证据链里明确登记的**具体文件**，绝不递归删除目录；
    - 删除前校验路径深度与可选前缀白名单，拒绝根目录和过短路径；
    - 测试模式下本模块只输出证据链，不产生任何写操作。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

Log = Callable[[str], None]


def _text(value: Any) -> str:
    """把枚举/数字/None 安全地转成字符串。"""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _as_list(value: Any) -> list[Any]:
    """把单条记录或 None 统一成列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """读取 ORM / Pydantic 对象属性，缺失时返回默认值。"""
    return getattr(obj, name, default)


def _is_file_path(value: Any) -> Optional[str]:
    """仅接受指向真实文件的路径，目录与空值一律拒绝。"""
    path = _text(value).strip()
    if not path or len(path) < 8:
        return None
    try:
        candidate = Path(path)
        if not candidate.is_file():
            return None
        parts = [p for p in candidate.parts if p not in ("", "/", "\\")]
        if len(parts) < 3:
            return None
        return str(candidate)
    except (OSError, ValueError):
        return None


class EvidenceBuilder:
    """以订阅为锚点，收集它产生的种子、下载文件、媒体库文件与历史记录。"""

    def __init__(self, log: Log) -> None:
        self._log = log

    @staticmethod
    def _call(oper: Any, method: str, *args: Any, **kwargs: Any) -> list[Any]:
        """
        调用 Oper 的查询方法并兼容签名差异。

        V3 宿主小版本间方法参数可能调整，因此先按关键字、再按位置尝试；
        任何异常都降级为空列表并交由调用方记录告警。
        """
        fn = getattr(oper, method, None)
        if not callable(fn):
            return []
        try:
            return _as_list(fn(**kwargs)) if kwargs else _as_list(fn(*args))
        except TypeError:
            try:
                return _as_list(fn(*args, **kwargs))
            except Exception:
                return []
        except Exception:
            return []

    def build(self, subscribe: Any) -> dict[str, Any]:
        """
        收集订阅的完整证据链。

        :return: 可序列化字典，字段包含 subscribe / hashes / torrents /
                 download_paths / media_paths / 各类 history id / warnings
        """
        sub_id = _attr(subscribe, "id")
        name = _text(_attr(subscribe, "name"))
        season = _attr(subscribe, "season")
        media_source = _text(_attr(subscribe, "media_source"))
        media_id = _text(_attr(subscribe, "media_id"))
        keyword = _text(_attr(subscribe, "keyword"))

        evidence: dict[str, Any] = {
            "subscribe": {
                "id": sub_id,
                "name": name,
                "season": season,
                "type": _text(_attr(subscribe, "type")),
                "media_source": media_source,
                "media_id": media_id,
                "keyword": keyword,
                "username": _text(_attr(subscribe, "username")),
            },
            "hashes": [],
            "downloaders": [],
            "torrents": [],
            "download_paths": [],
            "media_paths": [],
            "transfer_history_ids": [],
            "download_history_ids": [],
            "warnings": [],
        }

        def add_hash(value: Any) -> None:
            hash_value = _text(value).strip()
            if hash_value and hash_value not in evidence["hashes"]:
                evidence["hashes"].append(hash_value)

        def add_downloader(value: Any) -> None:
            name_value = _text(value).strip()
            if name_value and name_value not in evidence["downloaders"]:
                evidence["downloaders"].append(name_value)

        try:
            from app.db.oper.downloadhistory import DownloadHistoryOper
            from app.db.oper.transferhistory import TransferHistoryOper
        except Exception as error:
            evidence["warnings"].append(f"宿主历史 Oper 不可用：{error}")
            self._log(f"[订阅保种] 历史 Oper 导入失败，证据链仅含订阅本身：{error}")
            return evidence

        transfer_oper = TransferHistoryOper()
        download_oper = DownloadHistoryOper()

        # 1) 按规范媒体身份查询（V3 要求 media_source + media_id 成对使用）
        transfer_rows: list[Any] = []
        download_rows: list[Any] = []
        if media_source and media_id:
            transfer_rows += self._call(
                transfer_oper, "get_by_media_identity",
                media_source=media_source, media_id=media_id,
            )
            download_rows += self._call(
                download_oper, "get_by_media_identity",
                media_source=media_source, media_id=media_id,
            )
        # 2) 标题兜底：老订阅可能没有媒体身份，或历史记录未回填
        if name:
            transfer_rows += self._call(transfer_oper, "get_by_title", name)
            if hasattr(download_oper, "get_last_by"):
                download_rows += self._call(
                    download_oper, "get_last_by", title=name,
                )

        # 3) 汇总首轮哈希，再按哈希补齐关联记录
        for row in transfer_rows:
            add_hash(_attr(row, "download_hash"))
            add_downloader(_attr(row, "downloader"))
        for row in download_rows:
            add_hash(_attr(row, "download_hash"))
            add_downloader(_attr(row, "downloader"))

        for hash_value in list(evidence["hashes"]):
            transfer_rows += self._call(transfer_oper, "list_by_hash", hash_value)
            download_rows += self._call(download_oper, "get_by_hash", hash_value)
            for file_row in self._call(download_oper, "get_files_by_hash", hash_value):
                path = _is_file_path(
                    _attr(file_row, "fullpath") or _attr(file_row, "path")
                )
                if path and path not in evidence["download_paths"]:
                    evidence["download_paths"].append(path)

        # 4) 去重并提取全部具体文件路径（目录不进入删除清单）
        seen_transfer: set[Any] = set()
        for row in transfer_rows:
            row_id = _attr(row, "id")
            if row_id is not None and row_id not in seen_transfer:
                seen_transfer.add(row_id)
                evidence["transfer_history_ids"].append(row_id)
            src = _is_file_path(_attr(row, "src"))
            dest = _is_file_path(_attr(row, "dest"))
            if src and src not in evidence["download_paths"]:
                evidence["download_paths"].append(src)
            if dest and dest not in evidence["media_paths"]:
                evidence["media_paths"].append(dest)

        seen_download: set[Any] = set()
        for row in download_rows:
            row_id = _attr(row, "id")
            if row_id is not None and row_id not in seen_download:
                seen_download.add(row_id)
                evidence["download_history_ids"].append(row_id)
            path = _is_file_path(_attr(row, "path"))
            if path and path not in evidence["download_paths"]:
                evidence["download_paths"].append(path)

        # 5) 查询下载器中仍在的种子（只读，用于证据链与就绪确认）
        if evidence["hashes"]:
            try:
                from app.chain.download import DownloadChain

                chain = DownloadChain()
                torrents = chain.list_torrents(hashs=evidence["hashes"]) or []
                for torrent in _as_list(torrents):
                    evidence["torrents"].append({
                        "hash": _text(_attr(torrent, "hash")),
                        "title": _text(_attr(torrent, "title")),
                        "downloader": _text(_attr(torrent, "downloader")),
                        "progress": _attr(torrent, "progress", 0),
                    })
            except Exception as error:
                evidence["warnings"].append(f"下载器种子查询失败：{error}")

        if not evidence["hashes"]:
            evidence["warnings"].append(
                "未找到该订阅关联的下载 hash，L1 种子清理将被跳过（不会盲目按标题匹配）"
            )
        if not evidence["media_paths"]:
            evidence["warnings"].append("未找到媒体库具体文件，L3 将被跳过")

        self._log(
            f"[订阅保种] 证据链：订阅 #{sub_id} {name} S{season or '?'} | "
            f"hash {len(evidence['hashes'])} 个 | 下载器种子 {len(evidence['torrents'])} 个 | "
            f"下载侧文件 {len(evidence['download_paths'])} 个 | "
            f"媒体库文件 {len(evidence['media_paths'])} 个 | "
            f"转移记录 {len(evidence['transfer_history_ids'])} 条 | "
            f"下载历史 {len(evidence['download_history_ids'])} 条"
        )
        for path in evidence["download_paths"][:10]:
            self._log(f"[订阅保种]   下载侧 → {path}")
        for path in evidence["media_paths"][:10]:
            self._log(f"[订阅保种]   媒体库 → {path}")
        return evidence


class CleanupExecutor:
    """按 L0 → L1 → L3 → L2 顺序执行分级清理，逐步记录结果。"""

    def __init__(self, log: Log) -> None:
        self._log = log

    def _step(self, result: dict[str, Any], step: str, ok: bool, detail: str) -> None:
        """记录一步执行结果。"""
        result["steps"].append({"step": step, "ok": ok, "detail": detail})
        self._log(f"[订阅保种] {'✔' if ok else '✘'} {step}：{detail}")
        if not ok:
            result["errors"].append(f"{step}：{detail}")
            result["ok"] = False

    def _allowed(self, path: str, prefixes: list[str]) -> bool:
        """若配置了路径前缀白名单，则只删除落在白名单内的文件。"""
        if not prefixes:
            return True
        normalized = os.path.normcase(os.path.normpath(path))
        return any(
            os.path.normcase(os.path.normpath(p)) and
            normalized.startswith(os.path.normcase(os.path.normpath(p)))
            for p in prefixes
        )

    def _delete_files(
        self,
        paths: Iterable[str],
        prefixes: list[str],
        label: str,
        result: dict[str, Any],
    ) -> int:
        """逐个删除具体文件；目录、越界路径、白名单外路径全部跳过。"""
        deleted = 0
        for raw in paths:
            path = _is_file_path(raw)
            if not path:
                self._log(f"[订阅保种]   {label} 跳过（非具体文件）：{raw}")
                continue
            if not self._allowed(path, prefixes):
                self._log(f"[订阅保种]   {label} 跳过（不在路径白名单）：{path}")
                continue
            try:
                Path(path).unlink(missing_ok=True)
                deleted += 1
                self._log(f"[订阅保种]   {label} 已删除文件：{path}")
            except Exception as error:
                self._step(result, f"{label}删除 {os.path.basename(path)}", False, str(error))
        return deleted

    def run(
        self,
        evidence: dict[str, Any],
        actions: dict[str, Any],
        test_mode: bool = False,
    ) -> dict[str, Any]:
        """
        执行清理。

        :param evidence: EvidenceBuilder.build 的产物
        :param actions: 分级动作开关与路径白名单
        :param test_mode: True 时只输出证据链，不产生任何写操作
        """
        sub = evidence.get("subscribe", {})
        result: dict[str, Any] = {
            "subscribe_id": sub.get("id"),
            "subscribe_name": sub.get("name"),
            "season": sub.get("season"),
            "ok": True,
            "test_mode": test_mode,
            "steps": [],
            "errors": [],
            "evidence": evidence,
        }

        if test_mode:
            self._log(
                "[订阅保种] 【测试模式】以下动作均不会执行："
                f"删订阅={actions.get('delete_subscribe')} / "
                f"删种子={actions.get('delete_torrent')} / "
                f"删种子文件={actions.get('delete_torrent_files')} / "
                f"删本地文件={actions.get('delete_local_files')} / "
                f"删媒体库文件={actions.get('delete_media_files')} / "
                f"删历史记录={actions.get('delete_history')}"
            )
            self._step(result, "测试模式", True, "仅输出证据链，未进入删除流程")
            return result

        prefixes = [p.strip() for p in (actions.get("path_prefixes") or []) if p and p.strip()]
        hashes: list[str] = list(evidence.get("hashes") or [])
        sub_id = sub.get("id")

        # L0：先删除订阅，避免清理过程中宿主再次触发该订阅的搜索与补下
        if actions.get("delete_subscribe") and sub_id is not None:
            try:
                from app.db.oper.subscribe import SubscribeOper

                SubscribeOper().delete(int(sub_id))
                self._step(result, "L0 删除订阅", True, f"已删除订阅 #{sub_id} {sub.get('name')}")
            except Exception as error:
                self._step(result, "L0 删除订阅", False, str(error))

        # L1：通过宿主稳定下载链删除种子（qBittorrent / Transmission 均由宿主适配）
        if actions.get("delete_torrent") and hashes:
            try:
                from app.chain.download import DownloadChain

                chain = DownloadChain()
                delete_file = bool(actions.get("delete_torrent_files"))
                kwargs: dict[str, Any] = {"hashs": hashes, "delete_file": delete_file}
                downloaders = evidence.get("downloaders") or []
                if len(downloaders) == 1:
                    kwargs["downloader"] = downloaders[0]
                chain.remove_torrents(**kwargs)
                self._step(
                    result, "L1 删除种子", True,
                    f"已提交 {len(hashes)} 个 hash，下载器删文件={delete_file}",
                )
            except Exception as error:
                self._step(result, "L1 删除种子", False, str(error))

        # L1b：下载目录残留文件（默认关闭；绝不删目录）
        if actions.get("delete_local_files"):
            count = self._delete_files(
                evidence.get("download_paths") or [], prefixes, "L1b 下载文件", result
            )
            self._step(result, "L1b 下载文件", True, f"已删除 {count} 个文件")

        # L3：媒体库文件（默认关闭，风险最高，只删证据链登记的具体文件）
        if actions.get("delete_media_files"):
            count = self._delete_files(
                evidence.get("media_paths") or [], prefixes, "L3 媒体库文件", result
            )
            self._step(result, "L3 媒体库文件", True, f"已删除 {count} 个文件")

        # L2：转移记录与下载历史
        if actions.get("delete_history"):
            try:
                from app.db.oper.downloadhistory import DownloadHistoryOper
                from app.db.oper.transferhistory import TransferHistoryOper

                transfer_oper = TransferHistoryOper()
                for history_id in evidence.get("transfer_history_ids") or []:
                    try:
                        transfer_oper.delete(int(history_id))
                    except Exception as error:
                        self._step(result, f"L2 转移记录 #{history_id}", False, str(error))
                download_oper = DownloadHistoryOper()
                for history_id in evidence.get("download_history_ids") or []:
                    try:
                        download_oper.delete_history(int(history_id))
                    except Exception as error:
                        self._step(result, f"L2 下载历史 #{history_id}", False, str(error))
                self._step(
                    result, "L2 历史记录", True,
                    f"转移记录 {len(evidence.get('transfer_history_ids') or [])} 条 / "
                    f"下载历史 {len(evidence.get('download_history_ids') or [])} 条",
                )
            except Exception as error:
                self._step(result, "L2 历史记录", False, str(error))

        return result
