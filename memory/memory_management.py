#!/usr/bin/env python3
"""
记忆管理自动化脚本

功能：
- 解析L2 (global_mem.txt) 中的SECTION列表，同步到L1的L2关键词行
- 扫描L3 (memory/ 下的SOP/脚本/Skill目录)，同步到L1的L3极简索引
- 只添加缺失关键词，不搬运细节；默认不删除旧索引，只提示可能过期项

验收标准：
- L2变更时自动patch L1索引
- L3新增SOP/Skill/关键工具时自动patch L1索引
- 只添加关键词，不搬细节
- 不破坏现有L1结构

使用方法：
python memory_management.py              # 检查并同步
python memory_management.py --check      # 只检查不同步
python memory_management.py --dry-run    # 预览模式
python memory_management.py --rebuild-l3 # 按SOP>文件夹>独立py重建L3索引（显式清理重复项）

规则（来自META-SOP）：
- L1只写关键词/名称，禁搬细节
- 新增场景：L1加入极简关键词
- 删除场景：默认只提示stale，不自动删（避免误删已验证经验）
- 修改值：若不影响场景定位则不动L1
"""

import os
import re
import argparse
from datetime import datetime
from pathlib import Path

# Paths (relative to this file)
MEMORY_DIR = Path(__file__).resolve().parent
L1_PATH = MEMORY_DIR / "global_mem_insight.txt"
L2_PATH = MEMORY_DIR / "global_mem.txt"

# L3扫描排除项：排除缓存/数据/备份/密钥等（通用，不针对特定技能）
EXCLUDE_NAMES = {
    "__pycache__",
    "downloads",
    "L4_raw_sessions",
    "chat_history.json",
    "file_access_stats.json",
    "global_mem.txt",
    "global_mem_insight.txt",
    "memory_management_sop.md",
    "memory_management.py",  # 管理器自身不放L3索引，L0/L2已有入口
    "vision_api.template.py",
}
EXCLUDE_SUFFIXES = {".json", ".jsonl", ".txt", ".log", ".db", ".sqlite", ".pyc"}

# ── 解析辅助函数 ──

def parse_l2_sections(l2_content):
    """Parse L2 content to extract SECTION names. Section name should use safe tokens like OCR_VISION."""
    sections = []
    for line in l2_content.splitlines():
        match = re.match(r'^## \[([^\]]+)\]', line)
        if match:
            section = match.group(1).strip()
            if section:
                sections.append(section)
    return sections


def parse_l1_l2_topics(l1_content):
    """Parse L1 L2 line. L2 uses '/' as topic delimiter, so L2 section names must not contain '/'."""
    for line in l1_content.splitlines():
        if line.startswith('L2:'):
            parts = line.replace('L2:', '', 1).strip().split('/')
            return [p.strip() for p in parts if p.strip()]
    return []


def extract_l3_block(lines):
    """Return (start, end, block_lines) for L3 block. Continuation lines start with '|'."""
    start = None
    for i, line in enumerate(lines):
        if line.startswith('L3:'):
            start = i
            break
    if start is None:
        return None, None, []
    end = start + 1
    while end < len(lines) and lines[end].startswith('|'):
        end += 1
    return start, end, lines[start:end]


def parse_l1_l3_entries(l1_content):
    """Parse L1 L3 block into existing display entries."""
    lines = l1_content.splitlines()
    _, _, block = extract_l3_block(lines)
    if not block:
        return []
    entries = []
    for line in block:
        if line.startswith('L3:'):
            text = line.replace('L3:', '', 1).strip()
        else:
            text = line.strip().lstrip('|').strip()
        entries.extend([p.strip() for p in text.split('|') if p.strip()])
    return entries


def l3_base_name(path):
    """Return rough skill base for grouping SOP/folder/py."""
    name = path.name
    if path.is_dir():
        return name
    if name.endswith('_sop.md'):
        return name[:-7]  # remove _sop.md
    if name.endswith('.md'):
        return name[:-3]
    if name.endswith('.py'):
        return name[:-3]
    return name


def sop_represented_py(path, sop_bases):
    """Check if a .py file should be represented by an existing SOP.
    
    Two rules:
    1. Prefix match: py file name starts with any SOP base (e.g. vision_api -> vision_sop)
    2. Content scan: SOP document references the py file (e.g. vision_sop.md mentions ocr_utils)
    """
    if not path.is_file() or path.suffix != '.py':
        return False
    stem = path.stem  # e.g. vision_api, ocr_utils
    # Rule 1: prefix match
    for base in sop_bases:
        if stem == base or stem.startswith(base + '_'):
            return True
    # Rule 2: content scan - check if any SOP doc references this py file
    for sop_path in MEMORY_DIR.glob('*_sop.md'):
        try:
            content = sop_path.read_text(encoding='utf-8')
            # Match: import ocr_utils, ocr_utils.py, from ocr_utils, ocr_utils.
            if re.search(rf'\b{re.escape(stem)}\b', content):
                return True
        except Exception:
            continue
    return False


def should_ignore_l3_path(path):
    """Common ignore filter for L3 scan."""
    name = path.name
    if name.startswith('.') or name in EXCLUDE_NAMES:
        return True
    if path.is_file() and path.suffix in EXCLUDE_SUFFIXES:
        return True
    return False


def is_sop_file(path):
    """SOP文件判定：仅匹配 *_sop.md，与命名约定一致"""
    return path.is_file() and path.name.endswith('_sop.md') and not should_ignore_l3_path(path)


def is_folder_skill(path):
    """Check if directory qualifies as a skill folder.
    A folder is a skill only if it contains at least one .md or .py file inside.
    Empty folders, data-only folders (e.g. evolution with only .json/.jsonl),
    and backup folders are excluded.
    """
    if not path.is_dir() or should_ignore_l3_path(path):
        return False
    for child in path.iterdir():
        if child.is_file() and child.suffix in {'.md', '.py'}:
            return True
    return False


def is_standalone_py(path):
    """独立py文件判定：不被SOP代表的.py文件"""
    return path.is_file() and path.suffix == '.py' and not should_ignore_l3_path(path)


def scan_l3_entries():
    """Scan memory/ directory to discover L3 entries.
    
    Priority: SOP > folder > standalone py
    Returns list of display names for L3.
    """
    sop_bases = set()
    sop_names = []
    folder_names = []
    py_names = []
    
    # First pass: collect SOPs
    for item in sorted(MEMORY_DIR.iterdir()):
        if is_sop_file(item):
            base = l3_base_name(item)
            sop_bases.add(base)
            sop_names.append(base)
    
    # Second pass: collect folders and py files
    for item in sorted(MEMORY_DIR.iterdir()):
        if should_ignore_l3_path(item):
            continue
        if is_sop_file(item):
            continue  # already collected
        if is_folder_skill(item):
            folder_names.append(item.name)
        elif is_standalone_py(item):
            if not sop_represented_py(item, sop_bases):
                py_names.append(item.stem)
    
    return sop_names + folder_names + py_names


# ── 同步逻辑 ──

def sync_l2_to_l1(l1_content, l2_content):
    """Sync L2 sections to L1 L2 line. Returns (new_l1, added, removed_hints)."""
    l2_sections = parse_l2_sections(l2_content)
    l1_topics = parse_l1_l2_topics(l1_content)
    
    added = [s for s in l2_sections if s not in l1_topics]
    stale = [t for t in l1_topics if t not in l2_sections]
    
    if not added:
        return l1_content, added, stale
    
    # Patch L1 L2 line
    lines = l1_content.splitlines()
    new_lines = []
    for line in lines:
        if line.startswith('L2:'):
            current = line.replace('L2:', '', 1).strip()
            if current:
                new_topics = current + '/' + '/'.join(added)
            else:
                new_topics = '/'.join(added)
            new_lines.append(f'L2: {new_topics}')
        else:
            new_lines.append(line)
    
    return '\n'.join(new_lines), added, stale


def sync_l3_to_l1(l1_content, dry_run=False):
    """Sync L3 entries to L1 L3 block. Returns (new_l1, added, stale)."""
    l3_scan = scan_l3_entries()
    l3_existing = parse_l1_l3_entries(l1_content)
    
    added = [e for e in l3_scan if e not in l3_existing]
    stale = [e for e in l3_existing if e not in l3_scan]
    
    if not added or dry_run:
        return l1_content, added, stale
    
    # Patch L1 L3 block
    lines = l1_content.splitlines()
    start, end, block = extract_l3_block(lines)
    
    if start is None:
        # No L3 line, add one
        new_lines = lines + [f'L3: ' + ' | '.join(added)]
        return '\n'.join(new_lines), added, stale
    
    # Build new L3 content
    existing_text = ' | '.join(l3_existing + added)
    new_l3_line = f'L3: {existing_text}'
    
    new_lines = lines[:start] + [new_l3_line] + lines[end:]
    return '\n'.join(new_lines), added, stale


def rebuild_l3_index(l1_content):
    """Rebuild L3 index with priority: SOP > folder > standalone py.
    Removes entries that no longer exist in memory/ directory.
    """
    l3_scan = scan_l3_entries()
    l3_existing = parse_l1_l3_entries(l1_content)
    
    removed = [e for e in l3_existing if e not in l3_scan]
    
    lines = l1_content.splitlines()
    start, end, _ = extract_l3_block(lines)
    
    if start is None:
        if l3_scan:
            new_lines = lines + [f'L3: ' + ' | '.join(l3_scan)]
        else:
            new_lines = lines
        return '\n'.join(new_lines), removed
    
    # Build new L3 content with only valid entries
    valid_entries = [e for e in l3_existing if e in l3_scan]
    # Add new entries not in existing
    for e in l3_scan:
        if e not in valid_entries:
            valid_entries.append(e)
    
    if valid_entries:
        new_l3_line = f'L3: ' + ' | '.join(valid_entries)
    else:
        new_l3_line = 'L3: (empty)'
    
    new_lines = lines[:start] + [new_l3_line] + lines[end:]
    return '\n'.join(new_lines), removed


# ── 验证逻辑 ──

def validate_l1(l1_content):
    """Validate L1 structure. Returns list of issues."""
    issues = []
    lines = l1_content.splitlines()
    
    if len(lines) > 35:
        issues.append(f"L1 has {len(lines)} lines, exceeds 30-line limit")
    
    if len(l1_content) > 2000:
        issues.append(f"L1 is {len(l1_content)} chars, may exceed 1k tokens")
    
    # Check for forbidden patterns
    for i, line in enumerate(lines, 1):
        if re.search(r'(api[_-]?key|password|secret)\s*[:=]', line, re.IGNORECASE):
            issues.append(f"Line {i}: possible secret detected")
        if len(line) > 200:
            issues.append(f"Line {i}: too long ({len(line)} chars)")
    
    return issues


# ── 主入口 ──

def main():
    parser = argparse.ArgumentParser(description='Memory management automation')
    parser.add_argument('--check', action='store_true', help='Only check, do not sync')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without writing')
    parser.add_argument('--rebuild-l3', action='store_true', help='Rebuild L3 index with priority')
    parser.add_argument('--validate', action='store_true', help='Validate L1 structure')
    args = parser.parse_args()
    
    # Read files
    l1_content = L1_PATH.read_text(encoding='utf-8') if L1_PATH.exists() else ''
    l2_content = L2_PATH.read_text(encoding='utf-8') if L2_PATH.exists() else ''
    
    if args.validate:
        issues = validate_l1(l1_content)
        if issues:
            print("Validation issues:")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print("L1 validation passed")
        return
    
    if args.rebuild_l3:
        new_l1, removed = rebuild_l3_index(l1_content)
        if removed:
            print(f"Removed from L3: {', '.join(removed)}")
        if new_l1 != l1_content:
            if args.dry_run:
                print("[dry-run] Would update L1 with rebuilt L3 index")
            else:
                L1_PATH.write_text(new_l1, encoding='utf-8')
                print("L3 index rebuilt")
        else:
            print("L3 index already up to date")
        return
    
    # Normal sync mode
    # L2 sync
    new_l1, l2_added, l2_stale = sync_l2_to_l1(l1_content, l2_content)
    
    # L3 sync
    new_l1, l3_added, l3_stale = sync_l3_to_l1(new_l1, dry_run=args.dry_run or args.check)
    
    # Report
    if l2_added:
        print(f"L2 topics added: {', '.join(l2_added)}")
    if l3_added:
        print(f"L3 entries added: {', '.join(l3_added)}")
    if l2_stale:
        print(f"L2 stale (not auto-removed): {', '.join(l2_stale)}")
    if l3_stale:
        print(f"L3 stale (not auto-removed): {', '.join(l3_stale)}")
    
    if not l2_added and not l3_added:
        print("L1 already in sync")
        return
    
    if args.check:
        print("[check] Changes detected but not applied")
        return
    
    if args.dry_run:
        print("[dry-run] Would apply changes above")
        return
    
    # Write
    if new_l1 != l1_content:
        L1_PATH.write_text(new_l1, encoding='utf-8')
        print("L1 synced successfully")


if __name__ == '__main__':
    main()
