"""Render the same review claims and candidate status for human readers."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .common import frontmatter

LABELS = {
    "overview": ("今期の要点", "Overview"), "questions": ("質問と説明", "Questions and explanations"),
    "learning": ("学びと確認できた到達点", "Learning and demonstrated understanding"),
    "activities": ("実施したこと・確認の範囲", "Activities and verification"),
    "decisions": ("判断とその理由", "Decisions and reasons"),
    "unresolved": ("当時の未解決・未確認", "Unresolved or unverified at the time"),
    "insights": ("複数チャットを跨ぐ考察", "Cross-conversation insights"),
    "intelligence-candidate": ("Intelligence Layerへの候補", "Intelligence Layer candidates"),
    "ai-improvement": ("生成AIの挙動改善案", "Proposed AI behavior improvements"),
    "reusable-resource": ("再利用できる資料・手順", "Reusable resources and procedures"),
    "next-actions": ("次に試すこと", "Proposed next actions"),
}
BASIS = {"observed": "記録で確認", "reported": "発言・報告", "inferred": "推測", "proposed": "提案"}


def render_review(value: dict[str, Any], json_path: Path, language: str) -> str:
    en = language == "en"
    p = value["period"]
    titles = {"weekly": "週次", "monthly": "月次", "yearly": "年次"}
    title = (f"{p['kind'].title()} review" if en else titles[p["kind"]] + "レビュー") + f" — {p['key']}"
    lines = [frontmatter([
        ("type", "reviewNote"), ("schemaVersion", value["schemaVersion"]), ("id", value["id"]),
        ("title", title), ("periodKind", p["kind"]), ("periodStart", p["start"]),
        ("periodEndExclusive", p["endExclusive"]), ("utcOffsetMinutes", p["utcOffsetMinutes"]),
        ("generatedAt", value["generatedAt"]), ("reviewStatus", value["reviewStatus"]),
        ("coverageStatus", value["coverage"]["status"]), ("sourceFingerprint", value["inputFingerprint"]),
    ]), "", "# " + title, ""]
    end = (date.fromisoformat(p["endExclusive"]) - timedelta(days=1)).isoformat()
    lines += [f"{p['start']} — {end} (UTC offset {p['utcOffsetMinutes']:+d} minutes)", "",
              ("This review covers saved records, not a complete measure of life or work. Historical open issues "
               "are not necessarily current tasks. Intelligence candidates and improvements are unadopted proposals."
               if en else "保存済みの記録に基づくレビューです。生活・活動全体の網羅性を示すものではありません。"
               "当時の未解決事項を現在のタスクとはみなしません。Intelligence候補と改善案は採用前の内容です。"), ""]
    sources = {e["id"]: e for e in value["evidence"]}
    used = list(dict.fromkeys(id for item in value["items"] for id in item["evidenceIds"]))
    labels = {id: f"S{i:03d}" for i, id in enumerate(used, 1)}
    for category, headings in LABELS.items():
        items = [v for v in value["items"] if v["category"] == category]
        if not items:
            continue
        lines += ["## " + headings[int(en)], ""]
        for item in items:
            basis = item["basis"] if en else BASIS[item["basis"]]
            refs = " ".join(f"[{labels[id]}](#{labels[id].lower()})" for id in dict.fromkeys(item["evidenceIds"]))
            lines += [f"**{item['title']}** ({basis})", "", item["text"] + " " + refs, ""]
            if item["intelligenceKind"] != "none":
                lines += [("Candidate kind: " if en else "候補の種類：") + item["intelligenceKind"], ""]
            if item["caveat"]:
                lines += [("Limits / alternatives: " if en else "留保・別の解釈：") + item["caveat"], ""]
            if item["nextCheck"]:
                lines += [("Next check: " if en else "次の確認：") + item["nextCheck"], ""]
    if not value["items"]:
        lines += [("No reviewable evidence was found in the saved inputs for this period."
                   if en else "この期間について、保存済みの入力からレビューに使える根拠を確認できませんでした。"), ""]
    lines += ["## " + ("Coverage and limitations" if en else "対象範囲と不足情報"), "",
              f"- Input evidence: {value['coverage']['inputCount']}; weekly reviews: "
              f"{len(value['coverage']['weeklyInputs'])}; monthly reviews: "
              f"{len(value['coverage'].get('monthlyInputs', []))}; status: {value['coverage']['status']}."]
    for gap in value["coverage"]["gaps"][:12]:
        lines.append("- " + gap)
    if len(value["coverage"]["gaps"]) > 12:
        remaining = len(value["coverage"]["gaps"]) - 12
        lines.append(f"- {remaining} additional limitations are in JSON." if en
                     else f"- ほか{remaining}件の不足・留保はJSONに記録しています。")
    lines += ["", "## " + ("Sources" if en else "出典"), ""]
    for id in used:
        e = sources[id]
        excerpt = e.get("text", "")
        if len(excerpt) > 800:
            excerpt = excerpt[:800] + (" … [Full text in JSON.]" if en else " …［全文はJSONを参照］")
        lines += [f"### {labels[id]}", "", excerpt, ""]
        path = (e.get("rawSnapshotPath") or e.get("monthlyPath") or e.get("weeklyPath")
                or e.get("notePath") or e.get("canonicalPath"))
        if path:
            target = Path(path).as_posix() + (f":{e['rawLine']}" if e.get("rawLine") else "")
            lines += [f"[{e['kind']}](<{target}>)", ""]
        lines += [f"- Evidence ID: {id}", "- Recorded: " + ", ".join(e["timestamps"]), ""]
    lines += [f"[{'Structured data and full provenance' if en else '構造化データ・全出典'}]"
              f"(<{json_path.as_posix()}>)", ""]
    return "\n".join(lines)
