"""Render the same review claims and candidate status for human readers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import frontmatter

RENDER_VERSION = "3"


def render_review(value: dict[str, Any], json_path: Path, language: str) -> str:
    en = language == "en"
    p = value["period"]
    titles = {"weekly": "週次", "monthly": "月次", "yearly": "年次"}
    title = (f"{p['kind'].title()} review" if en else titles[p["kind"]] + "レビュー") + f" — {p['key']}"
    lines = [frontmatter([
        ("type", "reviewNote"), ("schemaVersion", value["schemaVersion"]), ("id", value["id"]),
        ("sourceId", value["sourceId"]),
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
    # One visible reference per source document; exact evidence IDs and snippets stay in JSON.
    groups: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    for id in used:
        e = sources[id]
        path = (e.get("canonicalPath") or e.get("rawSnapshotPath") or e.get("monthlyPath")
                or e.get("weeklyPath") or e.get("notePath"))
        key = str(path or id)
        if key not in groups:
            groups[key] = []
        groups[key].append(e)
        labels[id] = f"S{list(groups).index(key) + 1:03d}"

    def refs(item: dict[str, Any]) -> str:
        return " ".join(f"[{label}](#{label.lower()})"
                        for label in dict.fromkeys(labels[id] for id in item["evidenceIds"]))

    def cell(text: str) -> str:
        # Source text must not create extra Markdown columns or inject HTML.
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("|", "&#124;").replace("\r", "").replace("\n", "<br>"))

    def detail(item: dict[str, Any]) -> str:
        return str(item["text"]) + " " + refs(item)

    def table(title: str, categories: set[str], headers: list[str], *, candidate: bool = False) -> None:
        items = [item for item in value["items"] if item["category"] in categories]
        if not items:
            return
        lines.extend(["## " + title, "", "| " + " | ".join(headers) + " |",
                      "| " + " | ".join("---" for _ in headers) + " |"])
        for item in items:
            name = item["title"]
            if candidate:
                name += f" ({item['intelligenceKind']})"
            content = [cell(name), cell(item["text"]) + " " + refs(item)]
            limits = item["caveat"]
            if len(headers) == 4:
                content += [cell(limits) or "—", cell(item["nextCheck"]) or "—"]
            else:
                if item["nextCheck"]:
                    limits += (" " if limits else "") + ("Check: " if en else "確認案：") + item["nextCheck"]
                content.append(cell(limits) or "—")
            lines.append("| " + " | ".join(content) + " |")
        lines.append("")

    overview = [item for item in value["items"] if item["category"] == "overview"]
    for item in overview:
        lines.extend([detail(item), ""])
        if item["caveat"]:
            lines.extend([item["caveat"], ""])
        if item["nextCheck"]:
            lines.extend([("Check: " if en else "確認案：") + item["nextCheck"], ""])
    table("Questions and learning" if en else "質問・学び",
          {"questions", "learning"},
          ["Theme", "Question / demonstrated state", "Limits / check"] if en else
          ["テーマ", "質問・説明・確認できたこと", "残る点・確認案"])
    table("Activities, decisions and open questions" if en else "テーマ別の実施・判断・残る点",
          {"activities", "decisions", "unresolved"},
          ["Theme", "Outcome / decision and reason", "Remaining limits / check"] if en else
          ["テーマ", "実施・結果・判断と理由", "残る点・確認案"])
    insights = [item for item in value["items"] if item["category"] == "insights"]
    if insights:
        lines.extend(["## " + ("Insights and hypotheses" if en else "考察・仮説"), ""])
        for item in insights:
            lines.extend(["### " + item["title"], "", detail(item), "", item["caveat"], "",
                          item["nextCheck"], ""])
    table("Proposed AI behavior improvements" if en else "生成AIの挙動改善案", {"ai-improvement"},
          ["Observed problem", "Evidence and proposed change", "Limits", "How to verify"] if en else
          ["観察された問題", "根拠と改善案", "適用上の留保", "確認方法"])
    table("Intelligence Layer candidates" if en else "Intelligence Layerへの候補", {"intelligence-candidate"},
          ["Candidate", "Reusable content and scope", "Limits", "Before adoption"] if en else
          ["候補", "再利用する内容・適用範囲", "留保", "採用前の確認"], candidate=True)
    table("Resources and next experiments" if en else "再利用できる資料・次に試すこと",
          {"reusable-resource", "next-actions"},
          ["Subject", "Resource / proposed experiment", "Limits / check"] if en else
          ["対象", "資料・提案する実験", "留保・確認案"])
    if not value["items"]:
        lines += [("No reviewable evidence was found in the saved inputs for this period."
                   if en else "この期間について、保存済みの入力からレビューに使える根拠を確認できませんでした。"), ""]
    coverage = value["coverage"]
    lines += ["## " + ("Coverage and sources" if en else "対象範囲と出典"), "",
              (f"Input evidence units: {coverage['inputCount']}; weekly reviews: {len(coverage['weeklyInputs'])}; "
               f"monthly reviews: {len(coverage.get('monthlyInputs', []))}; status: {coverage['status']}." if en else
               f"入力の根拠単位：{coverage['inputCount']}件、週次：{len(coverage['weeklyInputs'])}件、"
               f"月次：{len(coverage.get('monthlyInputs', []))}件。対象範囲：{coverage['status']}。"), ""]
    if coverage["gaps"]:
        lines += [(f"There are {len(coverage['gaps'])} recorded coverage limitations; see JSON for all details."
                   if en else f"入力の不足・省略に関する記録が{len(coverage['gaps'])}件あります。"
                   "詳細はJSONに保存しています。"), ""]
    # Show consequential missing-source problems before repetitive truncation diagnostics.
    important = [g for g in coverage["gaps"] if any(word in g.lower() for word in
                 ("failed", "missing", "mismatch", "invalid", "unavailable", "excluded"))]
    for gap in list(dict.fromkeys(important))[:3]:
        lines.extend(["- " + cell(gap[:300]) + ("…" if len(gap) > 300 else "")])
    if important:
        lines.append("")
    lines += [("Source labels group citations by document; exact evidence IDs, snippets, timestamps and "
               "claim status are preserved for every item in JSON." if en else
               "出典番号は文書単位です。各項目に対応する正確な根拠ID・抜粋・日時・事実／推測／提案の区別は"
               "JSONに保持しています。考察は推測、改善案は提案として読んでください。"), "",
              f"[{'Structured data and full provenance' if en else '構造化データ・全出典・不足情報'}]"
              f"(<{json_path.as_posix()}>)", ""]
    for number, (path, records) in enumerate(groups.items(), 1):
        label = f"S{number:03d}"
        dates = sorted({datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(
            timezone(timedelta(minutes=p["utcOffsetMinutes"]))).date().isoformat()
            for e in records for t in e["timestamps"]})
        span = dates[0] + (" — " + dates[-1] if len(dates) > 1 else "")
        name = records[0].get("title") or records[0].get("label") or records[0]["kind"]
        lines += [f"### {label}", "", f"{cell(name)} · {span}", ""]
        if path not in sources:
            target = Path(path).as_posix()
            lines += [f"[{'Source document' if en else '出典文書'}](<{target}>)", ""]
    return "\n".join(lines)
