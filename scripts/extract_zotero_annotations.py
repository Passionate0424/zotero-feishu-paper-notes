#!/usr/bin/env python3
"""Read a paper, its PDF attachment, and annotations from Zotero Desktop.

The script is intentionally read-only and dependency-free. It talks only to
Zotero Desktop's local Web API, which defaults to http://127.0.0.1:23119.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = os.environ.get("ZOTERO_LOCAL_BASE_URL", "http://127.0.0.1:23119")
API_ROOT = "/api/users/0"
PAGE_LIMIT = 100
HEADERS = {"Zotero-API-Version": "3", "Accept": "application/json"}

COLOR_RULES: dict[str, tuple[str, str, str]] = {
    "#2ea8e5": ("key_point", "关键点", "mainline"),
    "#ffd400": ("emphasis", "强调", "mainline"),
    "#a28ae5": ("question", "疑问", "questions"),
    "#e56eee": ("question", "疑问（历史洋红色）", "questions"),
    "#5fb236": ("limitation", "缺陷/局限", "limitations"),
    "#ff6666": ("error", "疑似错误", "errors"),
    "#f19837": ("followup", "可追溯知识", "followups"),
}

QUEUE_NAMES = ("mainline", "questions", "limitations", "errors", "followups")
PAPER_EXCLUDED_TYPES = {"attachment", "annotation", "note"}


class ZoteroReadError(RuntimeError):
    """A controlled failure that can be rendered as JSON or Markdown."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        exit_code: int = 1,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.exit_code = exit_code
        self.details = details


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def creator_names(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for creator in data.get("creators") or []:
        name = creator.get("name") or " ".join(
            part for part in (creator.get("firstName"), creator.get("lastName")) if part
        )
        if name:
            names.append(str(name))
    return names


def year_from_date(value: Any) -> str | None:
    match = re.search(r"\b(18|19|20|21)\d{2}\b", str(value or ""))
    return match.group(0) if match else None


def item_summary(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") or item
    return {
        "key": item.get("key") or data.get("key"),
        "item_type": data.get("itemType"),
        "title": data.get("title") or "",
        "creators": creator_names(data),
        "year": year_from_date(data.get("date")),
    }


class ZoteroClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = self.base_url + path
        if params:
            clean = {key: value for key, value in params.items() if value is not None}
            url += "?" + urllib.parse.urlencode(clean)
        return url

    def get_raw(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[bytes, dict[str, str]]:
        request = urllib.request.Request(self._url(path, params), headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise ZoteroReadError(
                "api_error",
                f"Zotero API returned HTTP {exc.code} for {path}: {body}",
                exit_code=3,
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise ZoteroReadError(
                "api_unavailable",
                "Cannot reach Zotero Desktop local API. Start Zotero and enable the local API.",
                exit_code=3,
                details={"base_url": self.base_url, "error": str(exc)},
            ) from exc

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body, _ = self.get_raw(path, params)
        try:
            return json.loads(body.decode("utf-8", errors="replace") or "null")
        except json.JSONDecodeError as exc:
            raise ZoteroReadError(
                "invalid_response",
                f"Zotero returned non-JSON data for {path}.",
                exit_code=3,
            ) from exc

    def get_text(self, path: str) -> str:
        body, _ = self.get_raw(path)
        text = body.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(text)
            return str(parsed) if isinstance(parsed, str) else text
        except json.JSONDecodeError:
            return text.strip('"')

    def get_paged(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        start = 0
        rows: list[Any] = []
        base_params = dict(params or {})
        while True:
            page_params = {**base_params, "limit": PAGE_LIMIT, "start": start}
            body, headers = self.get_raw(path, page_params)
            try:
                page = json.loads(body.decode("utf-8", errors="replace") or "[]")
            except json.JSONDecodeError as exc:
                raise ZoteroReadError(
                    "invalid_response",
                    f"Zotero returned invalid paginated data for {path}.",
                    exit_code=3,
                ) from exc
            if not isinstance(page, list):
                raise ZoteroReadError(
                    "invalid_response",
                    f"Expected a list from Zotero for {path}.",
                    exit_code=3,
                )
            rows.extend(page)
            total_raw = headers.get("Total-Results")
            total = int(total_raw) if total_raw and total_raw.isdigit() else None
            start += len(page)
            if not page or (total is not None and start >= total) or len(page) < PAGE_LIMIT:
                break
        return rows


def resolve_paper(
    client: ZoteroClient, *, item_key: str | None, query: str | None
) -> dict[str, Any]:
    if item_key:
        item = client.get_json(f"{API_ROOT}/items/{urllib.parse.quote(item_key)}")
        data = item.get("data") or {}
        if data.get("itemType") == "attachment" and data.get("parentItem"):
            item = client.get_json(
                f"{API_ROOT}/items/{urllib.parse.quote(str(data['parentItem']))}"
            )
        if (item.get("data") or {}).get("itemType") in PAPER_EXCLUDED_TYPES:
            raise ZoteroReadError(
                "invalid_item",
                f"Item {item_key} is not a top-level paper item.",
                exit_code=2,
            )
        return item

    assert query is not None
    matches = client.get_paged(f"{API_ROOT}/items/top", {"q": query})
    matches = [
        item
        for item in matches
        if (item.get("data") or {}).get("itemType") not in PAPER_EXCLUDED_TYPES
        and (item.get("data") or {}).get("title")
    ]
    if not matches:
        raise ZoteroReadError(
            "no_match",
            f"No top-level Zotero paper matched: {query}",
            exit_code=2,
        )

    exact = [
        item
        for item in matches
        if normalize_title((item.get("data") or {}).get("title", "")) == normalize_title(query)
    ]
    candidates = exact or matches
    if len(candidates) != 1:
        raise ZoteroReadError(
            "ambiguous_match",
            f"The title query matched {len(candidates)} candidate papers; choose an item key.",
            exit_code=2,
            details={"candidates": [item_summary(item) for item in candidates[:20]]},
        )
    return candidates[0]


def resolve_pdf_attachment(
    client: ZoteroClient, paper_key: str, requested_key: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    children = client.get_paged(f"{API_ROOT}/items/{urllib.parse.quote(paper_key)}/children")
    attachments = [
        item for item in children if (item.get("data") or {}).get("itemType") == "attachment"
    ]
    pdfs = []
    for item in attachments:
        data = item.get("data") or {}
        title = str(data.get("title") or "")
        filename = str(data.get("filename") or "")
        if data.get("contentType") == "application/pdf" or title.lower().endswith(
            ".pdf"
        ) or filename.lower().endswith(".pdf"):
            pdfs.append(item)

    if not pdfs:
        raise ZoteroReadError(
            "missing_pdf",
            "The Zotero paper has no PDF attachment; full-paper reading cannot begin.",
            exit_code=4,
            details={"paper_key": paper_key, "attachments": [item_summary(x) for x in attachments]},
        )
    if requested_key:
        selected = [
            item
            for item in pdfs
            if str(item.get("key") or (item.get("data") or {}).get("key")) == requested_key
        ]
        if len(selected) != 1:
            raise ZoteroReadError(
                "invalid_pdf",
                f"Attachment {requested_key} is not a PDF child of paper {paper_key}.",
                exit_code=2,
                details={"paper_key": paper_key, "pdfs": [item_summary(x) for x in pdfs]},
            )
        return selected[0], attachments
    if len(pdfs) > 1:
        raise ZoteroReadError(
            "ambiguous_pdf",
            "The paper has multiple PDF attachments; rerun with --attachment-key.",
            exit_code=2,
            details={"paper_key": paper_key, "pdfs": [item_summary(x) for x in pdfs]},
        )
    return pdfs[0], attachments


def file_url_to_path(file_url: str) -> str | None:
    if not file_url.lower().startswith("file:"):
        return None
    parsed = urllib.parse.urlparse(file_url)
    path = urllib.parse.unquote(parsed.path)
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return str(Path(path))


def annotation_page_index(data: dict[str, Any]) -> int | None:
    raw = data.get("annotationPosition")
    if not raw:
        return None
    try:
        position = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    page_index = position.get("pageIndex") if isinstance(position, dict) else None
    return page_index + 1 if isinstance(page_index, int) else None


def normalize_annotation(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") or {}
    color = str(data.get("annotationColor") or "").lower()
    category, color_name, queue = COLOR_RULES.get(color, ("other", "其他", "other"))
    tags = [tag.get("tag") for tag in data.get("tags") or [] if tag.get("tag")]
    return {
        "key": item.get("key") or data.get("key"),
        "page": data.get("annotationPageLabel") or None,
        "page_index": annotation_page_index(data),
        "sort_index": data.get("annotationSortIndex") or "",
        "type": data.get("annotationType") or "",
        "color": color,
        "color_name": color_name,
        "category": category,
        "queue": queue,
        "text": compact_text(data.get("annotationText")),
        "comment": compact_text(data.get("annotationComment")),
        "tags": tags,
    }


def paper_payload(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") or {}
    return {
        "key": item.get("key") or data.get("key"),
        "item_type": data.get("itemType"),
        "title": data.get("title") or "",
        "creators": creator_names(data),
        "date": data.get("date") or "",
        "year": year_from_date(data.get("date")),
        "publication": data.get("publicationTitle")
        or data.get("proceedingsTitle")
        or data.get("conferenceName")
        or "",
        "doi": data.get("DOI") or "",
        "url": data.get("url") or "",
        "abstract": compact_text(data.get("abstractNote")),
    }


def build_payload(
    client: ZoteroClient, paper: dict[str, Any], attachment: dict[str, Any]
) -> dict[str, Any]:
    paper_key = str(paper.get("key") or (paper.get("data") or {}).get("key"))
    attachment_key = str(attachment.get("key") or (attachment.get("data") or {}).get("key"))
    attachment_data = attachment.get("data") or {}

    try:
        file_url = client.get_text(
            f"{API_ROOT}/items/{urllib.parse.quote(attachment_key)}/file/view/url"
        )
    except ZoteroReadError:
        file_url = str(attachment_data.get("url") or "")
    pdf_path = file_url_to_path(file_url)

    all_annotations = client.get_paged(f"{API_ROOT}/items", {"itemType": "annotation"})
    annotations = [
        normalize_annotation(item)
        for item in all_annotations
        if str((item.get("data") or {}).get("parentItem") or "") == attachment_key
    ]
    annotations.sort(key=lambda row: (row.get("sort_index") or "", row.get("key") or ""))

    by_color = Counter(row["color"] or "uncolored" for row in annotations)
    by_category = Counter(row["category"] for row in annotations)
    by_type = Counter(row["type"] or "unknown" for row in annotations)
    queues: dict[str, list[str]] = {name: [] for name in QUEUE_NAMES}
    for row in annotations:
        queue = row["queue"]
        if queue in queues:
            queues[queue].append(str(row["key"]))

    warnings: list[str] = []
    if not pdf_path:
        warnings.append("PDF attachment found, but Zotero did not expose a local file path.")
    elif not Path(pdf_path).exists():
        warnings.append(f"Zotero reported a PDF path that does not currently exist: {pdf_path}")

    return {
        "paper": paper_payload(paper),
        "item_key": paper_key,
        "attachment_key": attachment_key,
        "pdf_path": pdf_path,
        "file_url": file_url,
        "counts": {
            "total": len(annotations),
            "by_color": dict(sorted(by_color.items())),
            "by_category": dict(sorted(by_category.items())),
            "by_type": dict(sorted(by_type.items())),
        },
        "annotations": annotations,
        "queues": queues,
        "warnings": warnings,
    }


def markdown_annotation(row: dict[str, Any]) -> list[str]:
    page = row.get("page") or row.get("page_index") or "?"
    heading = f"### p.{page} · {row['color_name']} · `{row['key']}`"
    lines = [heading]
    if row.get("text"):
        lines.extend(["", f"> {row['text'].replace(chr(10), ' ')}"])
    if row.get("comment"):
        lines.extend(["", f"批注：{row['comment']}"])
    if not row.get("text") and not row.get("comment"):
        lines.extend(["", "（无文本批注）"])
    return lines


def render_markdown(payload: dict[str, Any]) -> str:
    paper = payload["paper"]
    counts = payload["counts"]
    lines = [
        f"# {paper['title']}",
        "",
        f"- Zotero item key: `{payload['item_key']}`",
        f"- PDF attachment key: `{payload['attachment_key']}`",
        f"- PDF path: `{payload.get('pdf_path') or 'unavailable'}`",
        f"- Authors: {', '.join(paper['creators']) or 'unknown'}",
        f"- Year: {paper.get('year') or 'unknown'}",
        f"- Annotations: {counts['total']}",
        "",
        "## 标注统计",
        "",
    ]
    for category, count in counts["by_category"].items():
        lines.append(f"- {category}: {count}")
    for warning in payload.get("warnings") or []:
        lines.extend(["", f"> 警告：{warning}"])

    section_names = {
        "mainline": "正文主线（蓝色与黄色）",
        "questions": "疑问与关键理解",
        "limitations": "缺陷与局限",
        "errors": "疑似错误",
        "followups": "可追溯知识",
        "other": "其他标注",
    }
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in section_names}
    for row in payload["annotations"]:
        grouped[row["queue"] if row["queue"] in grouped else "other"].append(row)
    for queue, title in section_names.items():
        if not grouped[queue]:
            continue
        lines.extend(["", f"## {title}", ""])
        for row in grouped[queue]:
            lines.extend(markdown_annotation(row))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def error_payload(exc: ZoteroReadError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"kind": exc.kind, "message": exc.message, "details": exc.details},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read a Zotero paper, PDF path, and color-routed annotations."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query", help="Paper title or a unique title fragment")
    source.add_argument("--item-key", help="Zotero paper item key (an attachment key is also accepted)")
    parser.add_argument("--attachment-key", help="Choose a specific PDF when a paper has multiple PDFs")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    client = ZoteroClient(args.base_url, timeout=args.timeout)
    try:
        paper = resolve_paper(client, item_key=args.item_key, query=args.query)
        paper_key = str(paper.get("key") or (paper.get("data") or {}).get("key"))
        attachment, _ = resolve_pdf_attachment(client, paper_key, args.attachment_key)
        payload = build_payload(client, paper, attachment)
    except ZoteroReadError as exc:
        if args.format == "json":
            print(json.dumps(error_payload(exc), ensure_ascii=False, indent=2))
        else:
            print(f"错误：{exc.message}", file=sys.stderr)
            if exc.details is not None:
                print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return exc.exit_code

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
