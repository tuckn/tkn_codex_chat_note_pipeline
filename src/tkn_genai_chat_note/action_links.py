"""`actions link --dry-run`: match recent conversations to Action notes with clues A-D only.

Clues (design 4.2): A the conversation opened or changed the Action file; B the Action's Notes cite
the conversation's session ID; C the person's request names the Action (WikiLink, file name, title
or alias); D the working folder's repository name matches exactly one target Action. A, B and C are
strong and link directly; a unique D links with medium strength; several D candidates are left for
the later AI step. An A that only names the file in a command's arguments is weak: it links only
together with another clue for the same Action. A conversation that links to nothing, but whose
repository matches a finished Action, is reported as follow-up work, a candidate for a new Action.
Nothing is written to the Vault and no generative AI is called. The report and digest cache are
written under `processes.state_root` and `processes.data_root`.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .chat_archives import conversation_files, open_archives
from .config import AppConfig
from .digests import DigestContext, load_or_build
from .path_text import PathText
from .runtime import PipelineError, atomic_write_json, atomic_write_text
from .vault_actions import ActionNote, load_actions

LOGGER = logging.getLogger(__name__)
REPORT_SCHEMA_VERSION = "1.1.0"
DECISIONS = ("linked", "ambiguous", "followup", "unlinked")
WIKILINK = re.compile(r"\[\[([^\]|#^]+)")


@dataclass
class Conversation:
    key: tuple[str, str, str]
    digest: dict[str, Any]
    subagents: list[dict[str, Any]] = field(default_factory=list)

    @property
    def session(self) -> dict[str, Any]:
        value: dict[str, Any] = self.digest["session"]
        return value

    def touches(self) -> list[dict[str, Any]]:
        items = list(self.session["vaultTouches"])
        for subagent in self.subagents:
            items.extend({**touch, "subagent": True} for touch in subagent["session"]["vaultTouches"])
        return items


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _token(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![0-9a-z_]){re.escape(name.casefold())}(?![0-9a-z_])")


class ActionIndex:
    def __init__(self, actions: list[ActionNote], actions_dir: str, min_chars: int) -> None:
        self.actions = actions
        self.by_path = {action.key(): action for action in actions}
        self.by_stem: dict[str, list[ActionNote]] = {}
        for action in actions:
            self.by_stem.setdefault(_nfc(action.stem), []).append(action)
        self.actions_dir = actions_dir
        self.names: list[tuple[str, str, ActionNote]] = []  # (kind, casefolded name, action)
        for action in actions:
            if len(_nfc(action.stem)) >= min_chars:
                self.names.append(("file", _nfc(action.stem), action))
            for kind, value in (("title", action.title), *(("alias", alias) for alias in action.aliases)):
                name = _nfc(value)
                if len(name) >= min_chars and name != _nfc(action.stem):
                    self.names.append((kind, name, action))

    def by_touch(self, path: str) -> ActionNote | None:
        """An Action from a Vault-relative path found in a tool call (normalized text)."""
        if not path.startswith(self.actions_dir + "/"):
            return None
        exact = self.by_path.get(_nfc(path))
        if exact is not None:
            return exact
        stem = path.rsplit("/", 1)[-1][: -len(".md")]
        matches = self.by_stem.get(_nfc(stem), [])
        return matches[0] if len(matches) == 1 else None


def _window(config: AppConfig, as_of: date | None, days: int) -> tuple[date, date, datetime]:
    zone = timezone(timedelta(minutes=config.utc_offset_minutes))
    end = as_of or datetime.now(zone).date()
    start = end - timedelta(days=days - 1)
    # One extra day: a file's modification time can precede its last message's local date.
    since = datetime.combine(start - timedelta(days=1), time(), zone)
    return start, end, since


def _check_roots(config: AppConfig, paths: PathText) -> None:
    """Refuse work folders that overlap an archive or the Vault, as written and as the file system resolves
    them (`..`, junctions and symbolic links of the existing part of a path)."""
    vault = config.vault.root
    protected = [settings.root for settings in config.archives.values()] + ([vault] if vault else [])
    for kind, root in (("data_root", config.processes.data_root), ("state_root", config.processes.state_root)):
        for folder in protected:
            for left, right in ((str(root), str(folder)), (str(root.resolve()), str(folder.resolve()))):
                if paths.is_within(left, right) or paths.is_within(right, left):
                    raise PipelineError(f"processes.{kind} must not overlap an archive or the Vault: {root}")


PASTED_TAG = re.compile(r"</?pasted_content\b[^>]*>", re.IGNORECASE)


def _first_line(text: str, limit: int) -> str:
    single = " ".join(PASTED_TAG.sub(" ", text).split())
    return single if len(single) <= limit else single[: limit - 1] + "…"


def _clue_c(conversation: Conversation, index: ActionIndex) -> dict[str, list[dict[str, Any]]]:
    """Actions named in the person's requests: WikiLinks, file names, titles and aliases."""
    found: dict[str, list[dict[str, Any]]] = {}
    requests = conversation.session["userRequests"]
    texts = [(request, _nfc(request["text"])) for request in requests]
    joined = "\n".join(text for _request, text in texts)
    for request, text in texts:
        for target in WIKILINK.findall(text):
            name = target.strip().rsplit("/", 1)[-1]
            name = name[:-3] if name.endswith(".md") else name
            for action in index.by_stem.get(_nfc(name), []):
                found.setdefault(action.key(), []).append(
                    {"clue": "C", "via": "wikilink", "evidenceId": request["evidenceId"]})
    for kind, name, action in index.names:
        if name not in joined:
            continue
        request = next(request for request, text in texts if name in text)
        items = found.setdefault(action.key(), [])
        if not any(item["via"] in {"wikilink", kind} for item in items):
            items.append({"clue": "C", "via": kind, "evidenceId": request["evidenceId"]})
    return found


def _clue_d(
    conversation: Conversation, targets: list[ActionNote], paths: PathText, vault_root: str,
    *, title_needs_identifier: bool = False,
) -> list[ActionNote]:
    session = conversation.session
    repo, cwd = session.get("repo") or "", session.get("cwd") or ""
    if not repo or (vault_root and paths.is_within(cwd, vault_root)):
        return []
    token = _token(repo)
    declared = [a for a in targets if any(_nfc(value) == _nfc(repo) for value in a.repositories)]
    if declared:
        return declared
    # Among the many finished Actions, a plain word such as "windows" would match unrelated titles.
    if title_needs_identifier and not re.search(r"[_-]", repo):
        return []
    return [a for a in targets if not a.repositories and token.search(_nfc(a.title))]


def _command_only(items: list[dict[str, Any]]) -> bool:
    """Only A clues that name the file in a command's arguments, without reading or changing it."""
    return all(item["clue"] == "A" and item["via"] == "command" for item in items)


def link_actions(config: AppConfig, *, dry_run: bool, as_of: date | None = None, days: int | None = None,
                 progress: Any = None) -> dict[str, Any]:
    if not dry_run:
        raise PipelineError("actions link writes nothing yet; run it with --dry-run (stage 0)")
    paths = PathText(config.path_aliases)
    _check_roots(config, paths)
    window_days = days or config.processes.actions.lookback_days
    start, end, since = _window(config, as_of, window_days)
    vault = config.vault
    actions, action_issues = load_actions(vault, paths, config.utc_offset_minutes)
    without_status = [a for a in actions if not a.status]
    if vault.actions.require_status:
        actions = [a for a in actions if a.status]
    vault_root = paths.folder(str(vault.root)) if vault.root else ""
    actions_dir = _nfc(vault.actions.dir)
    context = DigestContext(config.utc_offset_minutes, paths, vault_root, actions_dir,
                            tuple(tuple(group) for group in config.path_aliases))
    index = ActionIndex(actions, actions_dir, vault.actions.min_name_chars)
    scan = open_archives(config)
    run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z") + "-" + uuid4().hex[:6]
    digests: list[dict[str, Any]] = []
    digest_status: Counter[str] = Counter()
    unreadable: list[dict[str, str]] = []
    for archive in scan.archives:
        files = conversation_files(archive, modified_since=since)
        LOGGER.info("Archive %s (%s): %s conversation files changed since %s",
                    archive.name, archive.app, len(files), since.date().isoformat())
        for log in files:
            digest, status = load_or_build(log, archive, context, config.processes.data_root)
            digest_status[status.split(":", 1)[0]] += 1
            if digest is None:
                unreadable.append({"archive": log.archive, "relativePath": log.relative_path, "detail": status})
                continue
            if digest.get("session") is not None:
                digests.append(digest)
    conversations: dict[tuple[str, str, str], Conversation] = {}
    subagents: list[dict[str, Any]] = []
    kinds: Counter[str] = Counter()
    duplicates = 0
    for digest in digests:
        session = digest["session"]
        kinds[session["kind"]] += 1
        ref = session["sessionRef"]
        session_key = (ref["app"], ref["sourceId"], ref["sessionId"].casefold())
        if session["kind"] == "subagent":
            subagents.append(digest)
            continue
        if session["kind"] != "main":
            continue
        if session_key in conversations:
            duplicates += 1
            if digest["file"]["lineCount"] <= conversations[session_key].digest["file"]["lineCount"]:
                continue
        conversations[session_key] = Conversation(session_key, digest)
    orphan_subagents = 0
    for digest in subagents:
        session = digest["session"]
        ref = session["sessionRef"]
        parent = (ref["app"], ref["sourceId"], (session.get("parentSessionId") or "").casefold())
        if parent in conversations:
            conversations[parent].subagents.append(digest)
        else:
            orphan_subagents += 1
    window = {day.isoformat() for day in (start + timedelta(days=n) for n in range(window_days))}
    selected = [c for c in conversations.values() if window.intersection(c.session["localDates"])]
    selected.sort(key=lambda c: c.session.get("lastLocalTime") or "", reverse=True)
    touched_keys: set[str] = set()
    for conversation in selected:
        for touch in conversation.touches():
            if touch["access"] == "command":
                continue  # Naming a file in a command's arguments does not make its Action a target.
            action = index.by_touch(touch["path"])
            if action is not None:
                touched_keys.add(action.key())
    zone = timezone(timedelta(minutes=config.utc_offset_minutes))
    window_start = datetime.combine(start, time(), zone)
    targets = [a for a in actions if not a.closed and (
        a.in_progress or (a.updated is not None and a.updated >= window_start) or a.key() in touched_keys)]
    target_keys = {a.key() for a in targets}
    finished = [a for a in actions if a.closed]
    by_key = {a.key(): a for a in actions}
    rows: list[dict[str, Any]] = []
    clue_counts: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    for conversation in selected:
        session = conversation.session
        clues: dict[str, list[dict[str, Any]]] = {}
        for touch in conversation.touches():
            action = index.by_touch(touch["path"])
            if action is not None:
                clues.setdefault(action.key(), []).append(
                    {"clue": "A", "via": touch["access"], "evidenceId": touch["evidenceId"],
                     "subagent": bool(touch.get("subagent") or touch.get("sidechain"))})
        session_id = session["sessionRef"]["sessionId"].casefold()
        for action in actions:
            if session_id in action.notes_session_ids:
                clues.setdefault(action.key(), []).append({"clue": "B", "via": "notes-evidence"})
        for key, items in _clue_c(conversation, index).items():
            clues.setdefault(key, []).extend(items)
        d_candidates = _clue_d(conversation, targets, paths, vault_root)
        links: list[dict[str, Any]] = []
        weak: dict[str, dict[str, Any]] = {}
        for key, items in clues.items():
            action = by_key[key]
            link: dict[str, Any] = {"action": action.relative_path, "title": action.title, "status": action.status,
                    "target": key in target_keys, "strength": "strong",
                    "clues": sorted({item["clue"] for item in items}), "details": items}
            if _command_only(items):
                weak[key] = {**link, "strength": "weak"}
            else:
                links.append(link)
        finished_candidates: list[ActionNote] = []
        if links:
            decision = "linked"
        elif len(d_candidates) == 1:
            decision = "linked"
            action = d_candidates[0]
            details: list[dict[str, Any]] = [{"clue": "D", "via": "repositories" if action.repositories
                                              else "title", "repo": session["repo"]}]
            supporting = weak.pop(action.key(), None)  # A command naming the same Action backs the match.
            links.append({"action": action.relative_path, "title": action.title, "status": action.status,
                          "target": True, "strength": "medium", "clues": ["A", "D"] if supporting else ["D"],
                          "details": (supporting["details"] if supporting else []) + details})
        elif d_candidates:
            decision = "ambiguous"
        else:
            finished_candidates = _clue_d(conversation, finished, paths, vault_root, title_needs_identifier=True)
            decision = "followup" if finished_candidates else "unlinked"
        decisions[decision] += 1
        for link in links:
            clue_counts.update(link["clues"])
        requests = session["userRequests"]
        rows.append({
            "app": session["sessionRef"]["app"], "sourceId": session["sessionRef"]["sourceId"],
            "sessionId": session["sessionRef"]["sessionId"], "title": session["title"],
            "cwd": session["cwd"], "repo": session["repo"],
            "firstLocalTime": session["firstLocalTime"], "lastLocalTime": session["lastLocalTime"],
            "humanRequestCount": len(requests), "excludedUserMessages": session["excludedUserMessages"],
            "mediumConfidenceRequests": sum(1 for r in requests if r["confidence"] != "high"),
            "toolEventCount": len(session["toolEvents"]) + sum(len(s["session"]["toolEvents"])
                                                                for s in conversation.subagents),
            "subagentCount": len(conversation.subagents),
            "firstRequest": _first_line(requests[0]["text"], 100) if requests else "",
            "decision": decision, "links": links, "weakLinks": list(weak.values()),
            "dCandidates": [{"action": a.relative_path, "title": a.title} for a in d_candidates]
            if decision == "ambiguous" else [],
            "finishedActions": [{"action": a.relative_path, "title": a.title, "status": a.status}
                                for a in finished_candidates],
            "digest": {"archive": conversation.digest["file"]["archive"],
                       "relativePath": conversation.digest["file"]["relativePath"],
                       "contentSha256": conversation.digest["file"]["contentSha256"]},
        })
    linked_actions: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for link in row["links"]:
            linked_actions.setdefault(link["action"], []).append(
                {"sessionId": row["sessionId"], "app": row["app"], "clues": link["clues"], "title": row["title"]})
    summary = {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "runId": run_id,
        "dryRun": True,
        "asOf": end.isoformat(),
        "windowStart": start.isoformat(),
        "lookbackDays": window_days,
        "vaultWritten": False,
        "generativeAiUsed": False,
        "archives": [{"name": a.name, "app": a.app, "sourceId": a.view.source_id,
                      "ignoredManifests": len(a.view.ignored_manifests)} for a in scan.archives],
        "skippedArchives": scan.skipped,
        "digests": dict(digest_status),
        "unreadableFiles": unreadable,
        "sessionKinds": dict(kinds),
        "duplicateSessions": duplicates,
        "orphanSubagents": orphan_subagents,
        "conversationCount": len(rows),
        "decisions": {key: decisions.get(key, 0) for key in DECISIONS},
        "conversationsWithWeakLinks": sum(1 for r in rows if r["weakLinks"]),
        "linkedStrong": sum(1 for r in rows if any(link["strength"] == "strong" for link in r["links"])),
        "linkedMediumOnly": sum(1 for r in rows if r["links"] and all(link["strength"] == "medium"
                                                                         for link in r["links"])),
        "clueCounts": dict(sorted(clue_counts.items())),
        "actionCount": len(actions),
        "notesWithoutStatus": len(without_status),
        "targetActionCount": len(targets),
        "linkedActionCount": len(linked_actions),
        "actionIssues": action_issues,
        "conversationsWithoutHumanRequests": sum(1 for r in rows if not r["humanRequestCount"]),
    }
    state = config.processes.state_root / "actions-link" / run_id
    report = {**summary, "conversations": rows, "actions": linked_actions}
    atomic_write_json(state / "report.json", report)
    atomic_write_text(state / "links.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    atomic_write_text(state / "report.md", render_markdown(report, targets))
    atomic_write_json(config.processes.state_root / "actions-link" / "last-run.json",
                      {"runId": run_id, "reportPath": str(state / "report.md")})
    return {**summary, "ok": True, "complete": True, "reportPath": str(state / "report.md"),
            "reportJsonPath": str(state / "report.json"), "linksPath": str(state / "links.jsonl")}


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _stem(path: str) -> str:
    return Path(path).stem


def render_markdown(report: dict[str, Any], targets: list[ActionNote]) -> str:
    """Japanese report for the person; the JSON next to it is the machine-readable form."""
    decisions = report["decisions"]
    lines = [
        f"# actions link --dry-run（{report['windowStart']}〜{report['asOf']}、{report['lookbackDays']}日）",
        "",
        f"実行 `{report['runId']}`。Vaultには書いていない。生成AIは使っていない。手がかりはA〜Dだけ。",
        "",
        "## 1. 集計",
        "",
        "| 項目 | 件数 |",
        "| --- | --- |",
        f"| 期間内の会話（main） | {report['conversationCount']} |",
        f"| 対応づいた（A・B・Cのどれか） | {report['linkedStrong']} |",
        f"| 対応づいた（Dが1件だけ） | {report['linkedMediumOnly']} |",
        f"| 決まらない（Dが複数。段階1で生成AIに選ばせる） | {decisions['ambiguous']} |",
        f"| 終了したActionの続き（段階1で新しいActionの候補にする） | {decisions['followup']} |",
        f"| 対応なし | {decisions['unlinked']} |",
        f"| A(実行)だけの弱い手がかりがあった会話（対応にしない） | {report['conversationsWithWeakLinks']} |",
        f"| うち本人の発言のない会話 | {report['conversationsWithoutHumanRequests']} |",
        f"| 手がかりの件数（A・B・C・D） | {', '.join(f'{k} {v}' for k, v in report['clueCounts'].items()) or '0'} |",
        f"| Action（全体・対象・対応づいた） | {report['actionCount']}・{report['targetActionCount']}・"
        f"{report['linkedActionCount']} |",
        f"| 会話の要点（作った・再利用・読めない） | {report['digests'].get('built', 0)}・"
        f"{report['digests'].get('cached', 0)}・{report['digests'].get('unreadable', 0)} |",
        "",
        "対象のAction：statusが作業中、期間内に更新日時が変わった、または期間内の会話が開いた・変えたもの（終了したものを除く）。"
        "A・B・Cは対象の外のActionにも対応づける（表の「対象外」）。",
        "",
        "## 2. 会話ごと（新しい順）",
        "",
        "| 最後の発言 | アプリ | タイトル | リポジトリ | 判定 | Action（手がかり） | 最初の依頼 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    labels = {"linked": "対応", "ambiguous": "曖昧", "followup": "続き", "unlinked": "なし"}
    access_labels = {"read": "読", "edit": "変", "command": "実行"}

    def clue_text(link: dict[str, Any]) -> str:
        parts = []
        for clue in link["clues"]:
            if clue == "A":
                accesses = sorted({access_labels.get(item["via"], item["via"])
                                   for item in link["details"] if item["clue"] == "A"})
                parts.append(f"A({'・'.join(accesses)})")
            else:
                parts.append(clue)
        return "・".join(parts)

    for row in report["conversations"]:
        actions = "<br>".join(
            f"[[{_stem(link['action'])}]]（{clue_text(link)}"
            f"{'' if link['target'] else '、対象外'}{'、中' if link['strength'] == 'medium' else ''}）"
            for link in row["links"]
        )
        if row["decision"] == "ambiguous":
            actions = "候補：" + "<br>".join(f"[[{_stem(c['action'])}]]" for c in row["dCandidates"][:5])
        if row["decision"] == "followup":
            actions = "終了：" + "<br>".join(f"[[{_stem(a['action'])}]]（{a['status']}）"
                                           for a in row["finishedActions"][:3])
        weak = "<br>".join(f"弱い：[[{_stem(link['action'])}]]（A(実行)）" for link in row["weakLinks"])
        actions = "<br>".join(part for part in (actions, weak) if part)
        when = (row["lastLocalTime"] or "")[:16].replace("T", " ")
        lines.append(
            f"| {when} | {row['app']} | {_cell(row['title'] or row['sessionId'])} | {_cell(row['repo'])} | "
            f"{labels[row['decision']]} | {actions} | {_cell(row['firstRequest'])} |"
        )
    lines += ["", "## 3. Actionごと", "", "| Action | status | 会話 | 手がかり |", "| --- | --- | --- | --- |"]
    status_by_path = {a.relative_path: a.status for a in targets}
    for path, items in sorted(report["actions"].items(), key=lambda item: -len(item[1])):
        clue_set = sorted({clue for item in items for clue in item["clues"]})
        lines.append(f"| [[{_stem(path)}]] | {status_by_path.get(path, '（対象外）')} | {len(items)} | "
                     f"{'・'.join(clue_set)} |")
    unlinked_targets = [a for a in targets if a.relative_path not in report["actions"] and a.in_progress]
    lines += ["", f"作業中で、期間内の会話と対応づかなかったAction：{len(unlinked_targets)}件。", ""]
    lines += [
        "## 4. 読み方と限界",
        "",
        "- A：会話中にAIがActionのファイルを開いた・変えた（サブエージェントを含む。読＝読む・探す、変＝編集、"
        "実行＝コマンドの引数に現れた）。"
        "B：ActionのNotesの根拠にこの会話のセッションIDがある。"
        "C：本人の発言にActionのファイル名・WikiLink・title・aliasesがある。"
        "D：作業フォルダの最後のフォルダ名が、対象のActionの `repositories`（なければtitle）に1件だけ一致する。",
        "- A(実行)だけの手がかりは弱い。そのActionにほかの手がかりがあるときだけ対応にし、なければ「弱い」と示す。"
        "検索・一覧のコマンドで出たファイルは「読」に数える。",
        "- 続き：どのActionにも対応しない会話で、作業フォルダの名前が終了したActionの `repositories`"
        "（なければtitle。titleで比べるのは `_` か `-` を含む名前だけ）に一致したもの。"
        "段階1では、そのActionを再開する案ではなく、新しいActionの候補にする。",
        "- 会話はアーカイブから読むので、archiverがまだ取り込んでいない会話（使用中など）は入らない。",
        "- 本人の発言：Claude Codeは `turnOrigin` が `human` 以外の依頼文（別のセッションからの依頼など）を除く。"
        "古い記録でこの欄がないものは本人の発言として扱い、確度を1段下げる。",
        "- 詳細はこのフォルダの `report.json` と `links.jsonl`（会話ごとの手がかりと根拠ID）にある。",
        "",
    ]
    return "\n".join(lines)
