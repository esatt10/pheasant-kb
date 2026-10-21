//! Synapse Step 34.5: WASM-accelerated hot loops.
//!
//! Faithful Rust ports of two pure-Python functions, compiled to
//! `wasm32-unknown-unknown` and run inside the existing 34.1 sandbox
//! harness. Both are pure, deterministic, input->output transforms with
//! **no knowledge-base-specific data baked in** — the same compiled
//! `.wasm` binary works for every knowledge base; graph data is marshaled
//! in as a function argument on every call, the same way any compiled
//! Python extension (e.g. a C accelerator module) is a single generic
//! binary shared across all callers. This module ships as a vendored
//! package asset (`accel.wasm`, built from this crate), not as
//! deployment-specific configuration — see `docs/SYNAPSE_INTEGRATION.md`
//! §5 Step 34.5 for the full rationale and `loader.py` for how a fresh
//! process avoids paying full JIT-compilation cost on every call (AOT
//! module serialization, `wasmtime.Module.serialize`/`deserialize`).
//!
//! - `resolve_cross_source` mirrors `graph.enrichment.resolve_cross_source_edges`
//!   exactly, including both the `python_import` and `document_link`/`url`
//!   reference-type paths (the 34.4 spike ported only the former; this is
//!   the full production port — silently dropping the link-resolution path
//!   behind a config flag would be a correctness regression, not just a
//!   performance change).
//! - `scan_edges` mirrors `search.graph_search._scan_edges` exactly.
//!
//! Any change here must stay byte-for-byte equivalent to its Python
//! counterpart — `tests/test_wasm_accel.py` asserts parity on every
//! release, the same discipline the 34.4 benchmark spike used to validate
//! this port in the first place.

use std::alloc::{alloc, Layout};
use std::collections::{HashMap, HashSet};
use std::mem::forget;
use std::slice;

// ---- shared little-endian binary reader/writer -----------------------------

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Reader { data, pos: 0 }
    }
    fn u32(&mut self) -> u32 {
        let v = u32::from_le_bytes(self.data[self.pos..self.pos + 4].try_into().unwrap());
        self.pos += 4;
        v
    }
    fn string(&mut self) -> String {
        let len = self.u32() as usize;
        let s = String::from_utf8_lossy(&self.data[self.pos..self.pos + len]).into_owned();
        self.pos += len;
        s
    }
    fn opt_string(&mut self) -> Option<String> {
        let s = self.string();
        if s.is_empty() {
            None
        } else {
            Some(s)
        }
    }
}

struct Writer {
    buf: Vec<u8>,
}

impl Writer {
    fn new() -> Self {
        Writer { buf: Vec::new() }
    }
    fn u32(&mut self, v: u32) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }
    fn u8(&mut self, v: u8) {
        self.buf.push(v);
    }
    fn f64(&mut self, v: f64) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }
    fn string(&mut self, s: &str) {
        self.u32(s.len() as u32);
        self.buf.extend_from_slice(s.as_bytes());
    }
    fn opt_string(&mut self, s: &Option<String>) {
        self.string(s.as_deref().unwrap_or(""));
    }
    fn into_leaked(self) -> (*const u8, u32) {
        let len = self.buf.len() as u32;
        let boxed = self.buf.into_boxed_slice();
        let ptr = boxed.as_ptr();
        forget(boxed);
        (ptr, len)
    }
}

#[no_mangle]
pub extern "C" fn wasm_alloc(len: usize) -> *mut u8 {
    if len == 0 {
        return std::ptr::null_mut();
    }
    let layout = Layout::from_size_align(len, 1).unwrap();
    unsafe { alloc(layout) }
}

fn norm_rel(value: &str) -> String {
    value.replace('\\', "/").trim_matches('/').to_lowercase()
}

// ---- resolve_cross_source_edges --------------------------------------------

#[derive(Clone)]
struct ArtifactRef {
    node_id: String,
    source_id: String,
    relative_path: String,
}

fn match_suffix(candidate: &str, by_path: &HashMap<String, Vec<ArtifactRef>>) -> Vec<ArtifactRef> {
    let candidate = norm_rel(candidate);
    if let Some(direct) = by_path.get(&candidate) {
        if !direct.is_empty() {
            return direct.clone();
        }
    }
    let needle = format!("/{candidate}");
    let mut matches: Vec<ArtifactRef> = Vec::new();
    for (path, refs) in by_path.iter() {
        if path == &candidate || path.ends_with(&needle) {
            matches.extend(refs.iter().cloned());
        }
    }
    matches.sort_by(|a, b| {
        (&a.source_id, &a.relative_path, &a.node_id).cmp(&(&b.source_id, &b.relative_path, &b.node_id))
    });
    matches
}

fn resolve_python_import(module: &str, by_path: &HashMap<String, Vec<ArtifactRef>>) -> Vec<ArtifactRef> {
    let normalized = module.replace('\\', "/");
    let parts: Vec<&str> = normalized.split('.').filter(|p| !p.is_empty()).collect();
    if parts.is_empty() {
        return Vec::new();
    }
    let base = parts.join("/");
    for candidate in [format!("{base}.py"), format!("{base}/__init__.py")] {
        let matches = match_suffix(&candidate, by_path);
        if !matches.is_empty() {
            return matches;
        }
    }
    Vec::new()
}

/// Mirrors `_resolve_document_link` exactly, including Python's
/// `str.lstrip("./")` character-SET semantics (strip every leading `.` or
/// `/`, not the two-character literal prefix once) — a naive
/// `trim_start_matches("./")` port would silently under-strip a path like
/// `"../../foo.md"` and mismatch the reference.
fn resolve_document_link(target: &str, by_path: &HashMap<String, Vec<ArtifactRef>>) -> Vec<ArtifactRef> {
    if target.starts_with("http://") || target.starts_with("https://") || target.starts_with("mailto:") {
        return Vec::new();
    }
    let cleaned = target.split('#').next().unwrap_or("");
    let cleaned = cleaned.split('?').next().unwrap_or("").trim();
    if cleaned.is_empty() {
        return Vec::new();
    }
    let cleaned = cleaned.trim_start_matches(|c: char| c == '.' || c == '/');
    let norm = norm_rel(cleaned);
    if let Some(direct) = by_path.get(&norm) {
        if !direct.is_empty() {
            return direct.clone();
        }
    }
    // Obsidian-style wiki link without extension -> try common doc suffixes.
    let name = norm.rsplit('/').next().unwrap_or(&norm);
    if !name.contains('.') {
        for suffix in [".md", ".txt"] {
            let candidate = format!("{norm}{suffix}");
            if let Some(matches) = by_path.get(&candidate) {
                if !matches.is_empty() {
                    return matches.clone();
                }
            }
        }
    }
    match_suffix(&norm, by_path)
}

fn resolve_reference(reference: &str, reference_type: &Option<String>, by_path: &HashMap<String, Vec<ArtifactRef>>) -> Vec<ArtifactRef> {
    match reference_type.as_deref() {
        Some("python_import") => resolve_python_import(reference, by_path),
        Some("document_link") | Some("url") => resolve_document_link(reference, by_path),
        _ => Vec::new(),
    }
}

#[no_mangle]
pub extern "C" fn resolve_cross_source(input_ptr: *const u8, input_len: usize, out_len_ptr: *mut u32) -> *const u8 {
    let input = unsafe { slice::from_raw_parts(input_ptr, input_len) };
    let mut r = Reader::new(input);

    let artifact_count = r.u32();
    let mut by_path: HashMap<String, Vec<ArtifactRef>> = HashMap::new();
    for _ in 0..artifact_count {
        let node_id = r.string();
        let relative_path = r.string();
        let source_id = r.string();
        by_path
            .entry(norm_rel(&relative_path))
            .or_default()
            .push(ArtifactRef { node_id, source_id, relative_path });
    }

    let ext_count = r.u32();
    let mut ext_index: HashMap<String, (String, String)> = HashMap::new();
    for _ in 0..ext_count {
        let ext_id = r.string();
        let source_id = r.string();
        let reference = r.string();
        ext_index.insert(ext_id, (source_id, reference));
    }

    let edge_count = r.u32();
    #[allow(clippy::type_complexity)]
    let mut out: Vec<(String, String, String, String, String, String, Option<String>, bool)> = Vec::new();
    let mut seen: HashSet<(String, String, String)> = HashSet::new();
    for _ in 0..edge_count {
        let artifact_id = r.string();
        let ext_id = r.string();
        let edge_type = r.string();
        let reference_type = r.opt_string();

        let Some((source_id, reference)) = ext_index.get(&ext_id) else { continue };
        let targets = resolve_reference(reference, &reference_type, &by_path);
        for target in targets {
            if target.node_id == artifact_id {
                continue;
            }
            let same_source = &target.source_id == source_id;
            let key = (artifact_id.clone(), target.node_id.clone(), edge_type.clone());
            if seen.contains(&key) {
                continue;
            }
            seen.insert(key);
            out.push((
                artifact_id.clone(),
                target.node_id,
                edge_type.clone(),
                source_id.clone(),
                target.source_id.clone(),
                reference.clone(),
                reference_type.clone(),
                !same_source,
            ));
        }
    }
    out.sort_by(|a, b| (&a.0, &a.1, &a.2).cmp(&(&b.0, &b.1, &b.2)));

    let mut w = Writer::new();
    w.u32(out.len() as u32);
    for (source, target, etype, source_id, target_source_id, reference, reference_type, cross_source) in out {
        w.string(&source);
        w.string(&target);
        w.string(&etype);
        w.string(&source_id);
        w.string(&target_source_id);
        w.string(&reference);
        w.opt_string(&reference_type);
        w.u8(if cross_source { 1 } else { 0 });
    }
    let (ptr, len) = w.into_leaked();
    unsafe {
        *out_len_ptr = len;
    }
    ptr
}

// ---- graph_search._scan_edges -----------------------------------------------

const SKIP_KEYS: [&str; 8] = [
    "created_at",
    "updated_at",
    "knowledge_base_id",
    "hash",
    "text_hash",
    "sha256",
    "id",
    "key",
];

fn field_score(text: &str, tokens: &[String], q: &str, base_exact: f64, base_all: f64, base_any: f64) -> f64 {
    if text.is_empty() {
        return 0.0;
    }
    let low = text.to_lowercase();
    if !q.is_empty() && low == q {
        return base_exact;
    }
    if !q.is_empty() && low.contains(q) {
        return (base_exact + base_all) / 2.0;
    }
    if !tokens.is_empty() && tokens.iter().all(|t| low.contains(t.as_str())) {
        return base_all;
    }
    if !tokens.is_empty() && tokens.iter().any(|t| low.contains(t.as_str())) {
        return base_any;
    }
    0.0
}

#[no_mangle]
pub extern "C" fn scan_edges(input_ptr: *const u8, input_len: usize, out_len_ptr: *mut u32) -> *const u8 {
    let input = unsafe { slice::from_raw_parts(input_ptr, input_len) };
    let mut r = Reader::new(input);

    let token_count = r.u32();
    let tokens: Vec<String> = (0..token_count).map(|_| r.string()).collect();
    let q = r.string();
    let has_filter = r.string();
    let source_filter = if has_filter.is_empty() { None } else { Some(has_filter) };

    let edge_count = r.u32();
    let mut hits: Vec<(u32, f64)> = Vec::new();
    for edge_index in 0..edge_count {
        let source_label = r.string();
        let target_label = r.string();
        let edge_type = r.string();
        let edge_source_id = r.string();
        let attr_count = r.u32();
        let mut attrs: Vec<(String, String)> = Vec::with_capacity(attr_count as usize);
        for _ in 0..attr_count {
            let k = r.string();
            let v = r.string();
            attrs.push((k, v));
        }

        if let Some(filter) = &source_filter {
            if &edge_source_id != filter {
                continue;
            }
        }

        let type_score = field_score(&edge_type, &tokens, &q, 0.65, 0.55, 0.45);
        let endpoint_score = field_score(&source_label, &tokens, &q, 0.5, 0.4, 0.3)
            .max(field_score(&target_label, &tokens, &q, 0.5, 0.4, 0.3));
        let mut attr_score = 0.0f64;
        for (key, value) in &attrs {
            if SKIP_KEYS.contains(&key.as_str()) || key == "type" {
                continue;
            }
            attr_score = attr_score.max(field_score(value, &tokens, &q, 0.5, 0.35, 0.3));
        }
        let score = type_score.max(endpoint_score).max(attr_score);
        if score <= 0.0 {
            continue;
        }
        hits.push((edge_index, score));
    }

    let mut w = Writer::new();
    w.u32(hits.len() as u32);
    for (edge_index, score) in hits {
        w.u32(edge_index);
        w.f64(score);
    }
    let (ptr, len) = w.into_leaked();
    unsafe {
        *out_len_ptr = len;
    }
    ptr
}

// ---- PDF content-stream text scanner ---------------------------------------
//
// A faithful port of `pheasant.ingestion.extractor.scan_pdf_content_stream`.
// Unlike the two accelerators above, this one exists for **isolation, not
// speed**: PDF bytes reaching a region from a connector (a Drive file, a Slack
// upload, an IMAP attachment) are attacker-controllable, and a content-stream
// tokenizer is exactly the unbounded-loop, index-arithmetic-heavy code that
// wants a hard resource ceiling around it. Run through `WasmSandbox` it gets a
// fuel cap, a linear-memory cap, and zero host capabilities — no ambient env,
// filesystem, or network to reach even if the parse is subverted. See
// `pheasant/ingestion/extractor_sandbox.py` for the host side.
//
// Byte-for-byte parity with the Python reference is a hard requirement
// (`tests/test_document_extraction.py` asserts it on every build), which is
// why the three character-class tables and the cp1252 mapping below are
// transcribed from CPython's own answers rather than approximated with
// `is_ascii_alphabetic()` and friends: 65 byte values above 0x7F satisfy
// Python's `chr(b).isalpha()`, so an ASCII-only test would tokenize
// high-byte operands differently and silently drop text.

const ALPHA_MASK: [u64; 4] = [
    0x0000000000000000,
    0x07fffffe07fffffe,
    0x0420040000000000,
    0xff7fffffff7fffff,
];
const ALNUM_MASK: [u64; 4] = [
    0x03ff000000000000,
    0x07fffffe07fffffe,
    0x762c040000000000,
    0xff7fffffff7fffff,
];
const SPACE_MASK: [u64; 4] = [
    0x00000001f0003e00,
    0x0000000000000000,
    0x0000000100000020,
    0x0000000000000000,
];

// cp1252 code points for bytes 0x80..=0x9F (the only range where cp1252 and
// latin-1 disagree); 0xFFFD marks the five byte values cp1252 leaves
// undefined, matching Python's `errors="replace"`.
const CP1252_HIGH: [u32; 32] = [
    0x20AC, 0xFFFD, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021, 0x02C6, 0x2030, 0x0160, 0x2039,
    0x0152, 0xFFFD, 0x017D, 0xFFFD, 0xFFFD, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014,
    0x02DC, 0x2122, 0x0161, 0x203A, 0x0153, 0xFFFD, 0x017E, 0x0178,
];

#[inline]
fn in_mask(mask: &[u64; 4], b: u8) -> bool {
    (mask[(b >> 6) as usize] >> (b & 63)) & 1 == 1
}

fn decode_pdf_literal(raw: &[u8]) -> Vec<u8> {
    let mut out: Vec<u8> = Vec::new();
    let mut i = 0usize;
    while i < raw.len() {
        let byte = raw[i];
        if byte != 0x5C {
            out.push(byte);
            i += 1;
            continue;
        }
        i += 1;
        if i >= raw.len() {
            break;
        }
        let nxt = raw[i];
        match nxt {
            b'n' => {
                out.push(10);
                i += 1;
            }
            b'r' => {
                out.push(13);
                i += 1;
            }
            b't' => {
                out.push(9);
                i += 1;
            }
            b'b' => {
                out.push(8);
                i += 1;
            }
            b'f' => {
                out.push(12);
                i += 1;
            }
            b'(' => {
                out.push(40);
                i += 1;
            }
            b')' => {
                out.push(41);
                i += 1;
            }
            b'\\' => {
                out.push(92);
                i += 1;
            }
            0x30..=0x37 => {
                let mut value: u32 = 0;
                let mut digits = 0;
                while i < raw.len() && digits < 3 && (0x30..=0x37).contains(&raw[i]) {
                    value = value * 8 + (raw[i] - 0x30) as u32;
                    i += 1;
                    digits += 1;
                }
                out.push((value & 0xFF) as u8);
            }
            10 | 13 => {
                // Line continuation: emits nothing.
                let was_cr = nxt == 13;
                i += 1;
                if was_cr && i < raw.len() && raw[i] == 10 {
                    i += 1;
                }
            }
            other => {
                out.push(other);
                i += 1;
            }
        }
    }
    out
}

fn decode_pdf_text_bytes(raw: &[u8]) -> String {
    if raw.len() >= 2 && ((raw[0] == 0xFE && raw[1] == 0xFF) || (raw[0] == 0xFF && raw[1] == 0xFE)) {
        let big = raw[0] == 0xFE;
        let units: Vec<u16> = raw[2..]
            .chunks_exact(2)
            .map(|c| {
                if big {
                    u16::from_be_bytes([c[0], c[1]])
                } else {
                    u16::from_le_bytes([c[0], c[1]])
                }
            })
            .collect();
        // `filter_map(Result::ok)` drops unpaired surrogates, matching
        // Python's `errors="ignore"`.
        return char::decode_utf16(units).filter_map(|r| r.ok()).collect();
    }
    let mut out = String::with_capacity(raw.len());
    for &b in raw {
        let cp = if b < 0x80 {
            b as u32
        } else if b < 0xA0 {
            CP1252_HIGH[(b - 0x80) as usize]
        } else {
            b as u32
        };
        out.push(char::from_u32(cp).unwrap_or('\u{FFFD}'));
    }
    out
}

fn scan_pdf_content_stream(stream: &[u8]) -> String {
    let mut out: Vec<String> = Vec::new();
    let mut pending: Vec<String> = Vec::new();
    let n = stream.len();
    let mut i = 0usize;

    while i < n {
        let byte = stream[i];
        if byte == 0x28 {
            // '(' literal string, honouring balanced parens and escapes.
            let mut depth: i32 = 1;
            i += 1;
            let start = i;
            while i < n && depth != 0 {
                if stream[i] == 0x5C {
                    i += 2;
                    continue;
                }
                if stream[i] == 0x28 {
                    depth += 1;
                } else if stream[i] == 0x29 {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                i += 1;
            }
            let literal = &stream[start.min(n)..i.min(n)];
            let decoded = decode_pdf_text_bytes(&decode_pdf_literal(literal));
            i += 1;
            pending.push(decoded);
        } else if byte == 0x3C && i + 1 < n && stream[i + 1] != 0x3C {
            // '<' hex string
            match stream[i + 1..].iter().position(|&c| c == b'>') {
                None => break,
                Some(offset) => {
                    let end = i + 1 + offset;
                    let mut digits: Vec<u8> = stream[i + 1..end]
                        .iter()
                        .copied()
                        .filter(|&c| !in_mask(&SPACE_MASK, c))
                        .collect();
                    if digits.len() % 2 == 1 {
                        digits.push(b'0');
                    }
                    let mut bytes: Vec<u8> = Vec::with_capacity(digits.len() / 2);
                    let mut ok = true;
                    for pair in digits.chunks(2) {
                        match (hex_val(pair[0]), hex_val(pair[1])) {
                            (Some(hi), Some(lo)) => bytes.push(hi * 16 + lo),
                            _ => {
                                ok = false;
                                break;
                            }
                        }
                    }
                    if ok {
                        pending.push(decode_pdf_text_bytes(&bytes));
                    }
                    i = end + 1;
                }
            }
        } else if byte == 0x25 {
            // '%' comment runs to end of line.
            while i < n && stream[i] != 10 && stream[i] != 13 {
                i += 1;
            }
        } else if in_mask(&ALPHA_MASK, byte) || byte == 0x27 || byte == 0x22 {
            let start = i;
            while i < n
                && (in_mask(&ALNUM_MASK, stream[i])
                    || stream[i] == 0x2A
                    || stream[i] == 0x27
                    || stream[i] == 0x22)
            {
                i += 1;
            }
            let op = &stream[start..i];
            if op == b"Tj" || op == b"'" || op == b"\"" {
                if !pending.is_empty() {
                    out.push(pending.concat());
                    pending.clear();
                }
                if op == b"'" || op == b"\"" {
                    out.push(String::new());
                }
            } else if op == b"TJ" {
                if !pending.is_empty() {
                    out.push(pending.concat());
                    pending.clear();
                }
            } else if op == b"Td" || op == b"TD" || op == b"T*" || op == b"ET" {
                // flush_line()
                if !pending.is_empty() {
                    out.push(pending.concat());
                    pending.clear();
                } else if !out.is_empty() && !out[out.len() - 1].is_empty() {
                    out.push(String::new());
                }
            } else {
                pending.clear();
            }
        } else {
            i += 1;
        }
    }

    if !pending.is_empty() {
        out.push(pending.concat());
    }
    out.join("\n")
}

#[inline]
fn hex_val(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

/// Scan one inflated PDF content stream, returning UTF-8 text bytes.
#[no_mangle]
pub extern "C" fn pdf_scan_text(
    input_ptr: *const u8,
    input_len: usize,
    out_len_ptr: *mut u32,
) -> *const u8 {
    let input = unsafe { slice::from_raw_parts(input_ptr, input_len) };
    let bytes = scan_pdf_content_stream(input).into_bytes();
    let len = bytes.len() as u32;
    let boxed = bytes.into_boxed_slice();
    let ptr = boxed.as_ptr();
    forget(boxed);
    unsafe {
        *out_len_ptr = len;
    }
    ptr
}

// ---------------------------------------------------------------------------
// PDF tokenizer: the guest half of `pheasant.ingestion.pdf_objects.lex_pdf`.
// ---------------------------------------------------------------------------
//
// Every PDF syntax the equation reader parses (object dictionaries, object
// streams, CMaps, content streams) is tokenized here, so attacker-controlled
// syntax never meets a host-side parser. Output is the Python reference's
// `serialize_tokens`: one kind byte, a little-endian u32 length, the payload.
// `tests/test_pdf_math_extraction.py` holds the two byte-for-byte equal.

const TOK_NUMBER: u8 = 1;
const TOK_NAME: u8 = 2;
const TOK_STRING: u8 = 3;
const TOK_HEXSTRING: u8 = 4;
const TOK_KEYWORD: u8 = 5;
const TOK_ARRAY_OPEN: u8 = 6;
const TOK_ARRAY_CLOSE: u8 = 7;
const TOK_DICT_OPEN: u8 = 8;
const TOK_DICT_CLOSE: u8 = 9;
const TOK_BRACE_OPEN: u8 = 10;
const TOK_BRACE_CLOSE: u8 = 11;
const MAX_LEX_TOKENS: usize = 4_000_000;

#[inline]
fn pdf_whitespace(b: u8) -> bool {
    matches!(b, 0x00 | 0x09 | 0x0A | 0x0C | 0x0D | 0x20)
}

#[inline]
fn pdf_delimiter(b: u8) -> bool {
    matches!(b, b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%')
}

#[inline]
fn is_hex_digit(b: u8) -> bool {
    hex_val(b).is_some()
}

fn push_token(out: &mut Vec<u8>, kind: u8, value: &[u8]) {
    out.push(kind);
    out.extend_from_slice(&(value.len() as u32).to_le_bytes());
    out.extend_from_slice(value);
}

fn literal_end(data: &[u8], start: usize) -> usize {
    let n = data.len();
    let mut depth: i32 = 1;
    let mut i = start;
    while i < n {
        let b = data[i];
        if b == 0x5C {
            i += 2;
            continue;
        }
        if b == 0x28 {
            depth += 1;
        } else if b == 0x29 {
            depth -= 1;
            if depth == 0 {
                return i;
            }
        }
        i += 1;
    }
    n
}

fn decode_name(raw: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0usize;
    while i < raw.len() {
        if raw[i] == 0x23 && i + 2 < raw.len() && is_hex_digit(raw[i + 1]) && is_hex_digit(raw[i + 2]) {
            out.push(hex_val(raw[i + 1]).unwrap() * 16 + hex_val(raw[i + 2]).unwrap());
            i += 3;
        } else {
            out.push(raw[i]);
            i += 1;
        }
    }
    out
}

fn is_pdf_number(word: &[u8]) -> bool {
    let mut i = 0usize;
    if i < word.len() && (word[i] == b'+' || word[i] == b'-') {
        i += 1;
    }
    let rest = &word[i..];
    if rest.is_empty() {
        return false;
    }
    if rest[0] == b'.' {
        return rest.len() > 1 && rest[1..].iter().all(|b| b.is_ascii_digit());
    }
    let mut j = 0usize;
    while j < rest.len() && rest[j].is_ascii_digit() {
        j += 1;
    }
    if j == 0 {
        return false;
    }
    if j == rest.len() {
        return true;
    }
    rest[j] == b'.' && rest[j + 1..].iter().all(|b| b.is_ascii_digit())
}

fn find_from(data: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if from >= data.len() || needle.len() > data.len() {
        return None;
    }
    data[from..].windows(needle.len()).position(|w| w == needle).map(|p| p + from)
}

fn skip_inline_image(data: &[u8], start: usize) -> usize {
    let n = data.len();
    let mut i = start + 1;
    while i < n {
        match find_from(data, b"EI", i) {
            None => return n,
            Some(j) => {
                let before = if j > 0 { data[j - 1] } else { 0x20 };
                let after = if j + 2 < n { data[j + 2] } else { 0x20 };
                if pdf_whitespace(before) && (pdf_whitespace(after) || pdf_delimiter(after)) {
                    return j + 2;
                }
                i = j + 2;
            }
        }
    }
    n
}

fn lex_pdf(data: &[u8]) -> Vec<u8> {
    let mut out: Vec<u8> = Vec::new();
    let n = data.len();
    let mut count = 0usize;
    let mut i = 0usize;
    while i < n && count < MAX_LEX_TOKENS {
        let b = data[i];
        if pdf_whitespace(b) {
            i += 1;
        } else if b == b'%' {
            while i < n && data[i] != 10 && data[i] != 13 {
                i += 1;
            }
        } else if b == b'(' {
            let end = literal_end(data, i + 1);
            let slice = &data[(i + 1).min(n)..end.min(n)];
            push_token(&mut out, TOK_STRING, &decode_pdf_literal(slice));
            count += 1;
            i = end + 1;
        } else if b == b'<' {
            if i + 1 < n && data[i + 1] == b'<' {
                push_token(&mut out, TOK_DICT_OPEN, b"");
                count += 1;
                i += 2;
                continue;
            }
            let end = find_from(data, b">", i + 1).unwrap_or(n);
            let mut digits: Vec<u8> = data[(i + 1).min(n)..end].iter().copied().filter(|c| is_hex_digit(*c)).collect();
            if digits.len() % 2 == 1 {
                digits.push(b'0');
            }
            let bytes: Vec<u8> = digits
                .chunks_exact(2)
                .map(|p| hex_val(p[0]).unwrap() * 16 + hex_val(p[1]).unwrap())
                .collect();
            push_token(&mut out, TOK_HEXSTRING, &bytes);
            count += 1;
            i = end + 1;
        } else if b == b'>' {
            if i + 1 < n && data[i + 1] == b'>' {
                push_token(&mut out, TOK_DICT_CLOSE, b"");
                count += 1;
                i += 2;
            } else {
                i += 1;
            }
        } else if b == b'[' || b == b']' || b == b'{' || b == b'}' {
            let kind = match b {
                b'[' => TOK_ARRAY_OPEN,
                b']' => TOK_ARRAY_CLOSE,
                b'{' => TOK_BRACE_OPEN,
                _ => TOK_BRACE_CLOSE,
            };
            push_token(&mut out, kind, b"");
            count += 1;
            i += 1;
        } else if b == b'/' {
            let start = i + 1;
            i = start;
            while i < n && !pdf_whitespace(data[i]) && !pdf_delimiter(data[i]) {
                i += 1;
            }
            push_token(&mut out, TOK_NAME, &decode_name(&data[start..i]));
            count += 1;
        } else if b == b')' {
            i += 1;
        } else {
            let start = i;
            while i < n && !pdf_whitespace(data[i]) && !pdf_delimiter(data[i]) {
                i += 1;
            }
            let word = &data[start..i];
            if is_pdf_number(word) {
                push_token(&mut out, TOK_NUMBER, word);
            } else {
                push_token(&mut out, TOK_KEYWORD, word);
                if word == b"ID" {
                    i = skip_inline_image(data, i);
                }
            }
            count += 1;
        }
    }
    out
}

/// Tokenize PDF syntax, returning the serialized token stream.
#[no_mangle]
pub extern "C" fn pdf_lex(input_ptr: *const u8, input_len: usize, out_len_ptr: *mut u32) -> *const u8 {
    let input = unsafe { slice::from_raw_parts(input_ptr, input_len) };
    let bytes = lex_pdf(input);
    let len = bytes.len() as u32;
    let boxed = bytes.into_boxed_slice();
    let ptr = boxed.as_ptr();
    forget(boxed);
    unsafe {
        *out_len_ptr = len;
    }
    ptr
}
