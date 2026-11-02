#!/usr/bin/env swift
// 批量 OCR：macOS Vision → 每张 PNG 一个同名 .txt（可选独立输出目录）+ .meta.json 侧车
//
// 用法：
//   swift ocr.swift <inputDir> [outputDir] [--force] [--no-meta]
//   swiftc -O ocr.swift -o ocr && ./ocr <inputDir> [outputDir] [--force]
//
//   <inputDir>   递归扫描所有 .png
//   [outputDir]  .txt/.meta.json 的输出根目录（默认与 PNG 同目录）
//   --force      即使 .txt 已存在也重新识别（默认跳过 → 增量 OCR）
//   --no-meta    不写 .meta.json 侧车
//   --bbox       额外写 <base>.lines.json（每行的文本 + bbox），用于阅读序/双栏检测
//
// 产物：
//   <outputDir>/<相对路径>.txt        OCR 文本（阅读序，每视觉行一行）
//   <outputDir>/<相对路径>.meta.json  {chars, lines, mean_conf, low_conf_ratio,
//                                      col_spanning_lines, col_spanning_ratio, ms, w, h}
//   <outputDir>/<相对路径>.lines.json  每行文本 + bbox（仅 --bbox），阅读序/双栏诊断
//   <outputRoot>/_ocr_index.jsonl     每页一行汇总，供质量闸门 / 覆盖率统计
//
// 相比 ../weread-screenshot/ocr.swift 的改动：
//   1. 递归遍历 —— 采集目录是 <root>/<书名>/<NNN_章名>/page_NNNN.png（深两层）
//   2. 可选独立输出目录 —— 设计文档要求 screenshots/ 与 ocr_pages/ 分离
//   3. 每页 confidence 侧车 + JSONL 索引 —— 质量闸门的数据源（原版把 confidence 丢了）
//   4. 幂等 —— .txt 已存在则跳过（支撑「增量分页 OCR」节点）；--force 强制重跑
//   5. ★ 0 条识别结果时也写空 .txt —— 原版直接 return 不写文件，
//      「已处理过没有」的判断会永远命中不到，导致无限重跑
//   6. ★ 像素自适应行聚类阈值 —— 原版固定 0.012（归一化高度），
//      在 3300px 高的长图上等价 40px，会把相邻两行并成一行
//   7. 中英混排 join —— 只在两侧都是 ASCII 字母/数字时补空格，
//      避免英文单词粘连，也不给中文插空格
//   8. recognitionLanguages 增加 zh-Hant —— 繁体书用 zh-Hans 纠正会把字形带偏
//      （实测《穷查理宝典》出现「专厲权利」：简体书的字被纠成繁体形）
//   9. 严格按 y 排序后再做行聚类 —— 原版的比较器不满足严格弱序，偶尔会乱序
//  10. ★ 双栏检测（col_spanning_*）—— 整视口截图含左右两栏时，Vision 会把两栏
//      同行并成一行，阅读序被打乱。实测旧版 1280 宽截图 30 页：每页都有，
//      8.2% 的行跨越栏间距；改用 canvas 单栏截图后为 0%
//  11. 路径前缀剥离前解析符号链接 —— macOS /tmp → /private/tmp，否则拼出
//      "private穷查理宝典" 这种路径
//  12. 索引文件先确保存在再 append —— 原来 try? 静默吞掉失败，索引永不落盘
//
// 性能（实测 M 系列 Mac，.accurate 级别）：
//   ~3.2 s/页；4 路并行仅 1.3×（Vision 走系统服务，瓶颈不在本进程 CPU）
//   → 不要在 OCR 内部做并行，要跟采集层做流水线并行

import Foundation
import Vision
import AppKit

// ============================== 参数解析 ==============================

func usage() -> Never {
    FileHandle.standardError.write(
        "usage: swift ocr.swift <inputDir> [outputDir] [--force] [--no-meta] [--bbox]\n"
            .data(using: .utf8)!)
    exit(2)
}

var positional: [String] = []
var flags: Set<String> = []
for a in CommandLine.arguments.dropFirst() {
    if a.hasPrefix("--") { flags.insert(String(a)) } else { positional.append(a) }
}
guard let inputDirPath = positional.first else { usage() }
// ★ 解析符号链接：macOS 的 /tmp → /private/tmp，不解析会导致相对路径前缀剥离失败
//   （曾把 "穷查理宝典" 拼成 "private穷查理宝典"）
let inputDir = URL(fileURLWithPath: inputDirPath, isDirectory: true).resolvingSymlinksInPath()
let outputRoot: URL? = positional.count >= 2
    ? URL(fileURLWithPath: positional[1], isDirectory: true).resolvingSymlinksInPath()
    : nil
let force = flags.contains("--force")
let writeMeta = !flags.contains("--no-meta")
let writeBBox = flags.contains("--bbox")

// ============================== 工具 ==============================

/// 递归找出所有 .png，按完整路径排序（page_0001 < page_0100 < page_0102 天然有序）
func findPNGs(_ root: URL) -> [URL] {
    let fm = FileManager.default
    guard let e = fm.enumerator(
        at: root,
        includingPropertiesForKeys: [.isRegularFileKey],
        options: [.skipsHiddenFiles, .skipsPackageDescendants]
    ) else { return [] }
    var out: [URL] = []
    for case let f as URL in e where f.pathExtension.lowercased() == "png" {
        out.append(f)
    }
    return out.sorted { $0.path < $1.path }
}

/// 相对路径（相对给定 root），剥离失败时退化为文件名
func relPath(_ url: URL, root: URL) -> String {
    let p = url.resolvingSymlinksInPath().standardizedFileURL.path
    let r0 = root.resolvingSymlinksInPath().standardizedFileURL.path
    if p.hasPrefix(r0) {
        var r = String(p.dropFirst(r0.count))
        while r.hasPrefix("/") { r.removeFirst() }
        if !r.isEmpty { return r }
    }
    return url.lastPathComponent
}

/// 输出路径：outputRoot 存在则镜像相对路径，否则与 PNG 同目录
func outputBase(for png: URL) -> URL {
    guard let outRoot = outputRoot else { return png.deletingPathExtension() }
    let relNoExt = (relPath(png, root: inputDir) as NSString).deletingPathExtension
    let dest = outRoot.appendingPathComponent(relNoExt)
    // 目录可能不存在（截图目录与 OCR 目录分离时）
    try? FileManager.default.createDirectory(
        at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
    return dest
}

/// 只在 ASCII 字母/数字交界处补空格：英文单词不粘连，中文不插空格
func joinTokens(_ parts: [String]) -> String {
    var out = ""
    for p in parts {
        guard let first = p.first else { continue }
        if let prev = out.last {
            let prevAlnum = prev.isASCII && (prev.isLetter || prev.isNumber)
            let nextAlnum = first.isASCII && (first.isLetter || first.isNumber)
            if prevAlnum && nextAlnum { out += " " }
        }
        out += p
    }
    return out
}

struct PageResult {
    let chars: Int
    let lines: Int
    let meanConf: Double
    let lowConfRatio: Double
    let ms: Int
    let skipped: Bool
    let colSpanning: Int
}

// ============================== 单页 OCR ==============================

func ocrOne(_ pngURL: URL, outBase: URL, writeMeta: Bool, writeBBox: Bool) throws -> PageResult {
    let t0 = Date()

    guard let image = NSImage(contentsOf: pngURL),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
    else {
        throw NSError(domain: "ocr", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "load failed"])
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    request.usesLanguageCorrection = true
    request.minimumTextHeight = 0.0

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    try handler.perform([request])

    let observations = request.results ?? []

    // 像素自适应行聚类容差：目标 ≈ 10 设备像素
    // （原版固定 0.012，在 3340px 高的长图上等价 40px，会误并相邻行）
    let h = CGFloat(cgImage.height)
    let tol = max(0.003, min(0.020, 10.0 / h))

    // 双栏检测阈值：一行内出现 > 12% 图宽的横向空隙 → 该行跨越了栏间距
    // （整视口截图含左右两栏时，Vision 会把两栏同行并成一行，阅读序被打乱）
    let gutterGap = 0.12

    struct Item { let y: CGFloat; let x0: CGFloat; let x1: CGFloat
                  let text: String; let conf: Float }
    var items: [Item] = []
    var confs: [Float] = []
    var lowConf = 0
    for obs in observations {
        guard let top = obs.topCandidates(1).first else { continue }
        let bb = obs.boundingBox
        items.append(Item(y: 1.0 - bb.maxY, x0: bb.minX, x1: bb.maxX,
                          text: top.string, conf: top.confidence))
        confs.append(top.confidence)
        if top.confidence < 0.5 { lowConf += 1 }
    }

    // 先严格按 y 排序，再做行聚类（比较器满足严格弱序）
    items.sort { $0.y == $1.y ? $0.x0 < $1.x0 : $0.y < $1.y }

    var lines: [[Item]] = []
    for it in items {
        if var last = lines.last, !last.isEmpty {
            let mean = last.reduce(0.0) { $0 + $1.y } / CGFloat(last.count)
            if abs(mean - it.y) <= tol {
                last.append(it)
                lines[lines.count - 1] = last
                continue
            }
        }
        lines.append([it])
    }

    // 每行：文本 + 是否跨越栏间距 + bbox
    struct LineInfo { let text: String; let x0: CGFloat; let x1: CGFloat
                      let y: CGFloat; let spans: Bool }
    var lineInfos: [LineInfo] = []
    for line in lines {
        let sorted = line.sorted { $0.x0 < $1.x0 }
        var maxGap: CGFloat = 0
        for i in 1..<max(sorted.count, 1) {
            maxGap = max(maxGap, sorted[i].x0 - sorted[i - 1].x1)
        }
        let text = joinTokens(sorted.map { $0.text })
        lineInfos.append(LineInfo(text: text,
                                  x0: sorted.first?.x0 ?? 0,
                                  x1: sorted.last?.x1 ?? 0,
                                  y: sorted.first?.y ?? 0,
                                  spans: maxGap > gutterGap))
    }
    let textLines = lineInfos.map { $0.text }
    let text = textLines.joined(separator: "\n")
    let spanning = lineInfos.filter { $0.spans }.count

    // ★ 即使 0 条识别结果也要写文件 —— 否则「是否已处理」的判断永远不成立
    let txtURL = outBase.appendingPathExtension("txt")
    try text.write(to: txtURL, atomically: true, encoding: .utf8)

    let meanConf = confs.isEmpty ? 0.0 : Double(confs.reduce(0, +)) / Double(confs.count)
    let lowRatio = confs.isEmpty ? 1.0 : Double(lowConf) / Double(confs.count)
    let ms = Int(Date().timeIntervalSince(t0) * 1000)

    if writeBBox {
        let rows: [[String: Any]] = lineInfos.map {
            ["text": $0.text, "x0": Double($0.x0), "x1": Double($0.x1),
             "y": Double($0.y), "spans_column": $0.spans]
        }
        if let data = try? JSONSerialization.data(
                withJSONObject: rows, options: [.sortedKeys, .withoutEscapingSlashes]),
           let s = String(data: data, encoding: .utf8) {
            try? s.write(to: outBase.appendingPathExtension("lines.json"),
                         atomically: true, encoding: .utf8)
        }
    }

    if writeMeta {
        let meta: [String: Any] = [
            "chars": text.count,
            "lines": textLines.count,
            "mean_conf": round(meanConf * 10000) / 10000,
            "low_conf_ratio": round(lowRatio * 10000) / 10000,
            "col_spanning_lines": spanning,
            "col_spanning_ratio": textLines.isEmpty ? 0 : round(Double(spanning) / Double(textLines.count) * 10000) / 10000,
            "ms": ms,
            "width": cgImage.width,
            "height": cgImage.height,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: meta,
                                                  options: [.prettyPrinted, .sortedKeys]),
           let s = String(data: data, encoding: .utf8) {
            try? s.write(to: outBase.appendingPathExtension("meta.json"),
                         atomically: true, encoding: .utf8)
        }
    }

    return PageResult(chars: text.count, lines: textLines.count,
                      meanConf: meanConf, lowConfRatio: lowRatio,
                      ms: ms, skipped: false, colSpanning: spanning)
}

// ============================== 主流程 ==============================

let fm = FileManager.default
guard fm.fileExists(atPath: inputDir.path) else {
    FileHandle.standardError.write("no such dir: \(inputDir.path)\n".data(using: .utf8)!)
    exit(1)
}
if let o = outputRoot {
    try? fm.createDirectory(at: o, withIntermediateDirectories: true)
}

let pngs = findPNGs(inputDir)
if pngs.isEmpty {
    print("No PNG files under \(inputDir.path)")
    exit(0)
}
print("Found \(pngs.count) PNG files under \(inputDir.path)\n")

let indexURL = (outputRoot ?? inputDir).appendingPathComponent("_ocr_index.jsonl")
var indexLines: [String] = []
let startTime = Date()
var done = 0, skipped = 0, failed = 0
var totalChars = 0

for (i, png) in pngs.enumerated() {
    autoreleasepool {
        let base = outputBase(for: png)
        let elapsed = Date().timeIntervalSince(startTime)
        let avgPerFile = elapsed / Double(max(1, done + skipped + failed))
        let eta = avgPerFile * Double(pngs.count - i - 1)
        let prefix = String(format: "[%4d/%4d] +%.0fs ETA %.0fs", i + 1, pngs.count, elapsed, eta)

        // 幂等：.txt 已存在则跳过（支撑增量 OCR）
        if !force, fm.fileExists(atPath: base.appendingPathExtension("txt").path) {
            skipped += 1
            if i % 20 == 0 || i == pngs.count - 1 {
                print("\(prefix)  ⏭  \(png.lastPathComponent) (已有 .txt，跳过)")
            }
            return
        }

        do {
            let r = try ocrOne(png, outBase: base, writeMeta: writeMeta, writeBBox: writeBBox)
            done += 1
            totalChars += r.chars
            let rel = relPath(png, root: inputDir)
            let row: [String: Any] = [
                "png": rel,
                "txt": relPath(base, root: outputRoot ?? inputDir),
                "chars": r.chars,
                "lines": r.lines,
                "mean_conf": round(r.meanConf * 10000) / 10000,
                "low_conf_ratio": round(r.lowConfRatio * 10000) / 10000,
                "col_spanning_lines": r.colSpanning,
                "ms": r.ms,
            ]
            if let data = try? JSONSerialization.data(withJSONObject: row,
                                                      options: [.sortedKeys, .withoutEscapingSlashes]),
               let s = String(data: data, encoding: .utf8) {
                indexLines.append(s)
            }
            let flag = r.lowConfRatio > 0.3 ? " ⚠️低置信" : ""
            let colFlag = r.colSpanning > 0 ? " ⚠️跨栏\(r.colSpanning)行" : ""
            print("\(prefix)  \(png.lastPathComponent) → \(r.chars) 字 / \(r.lines) 行 / conf \(String(format: "%.2f", r.meanConf))\(flag)\(colFlag)")
        } catch {
            failed += 1
            print("\(prefix)  ❌ \(png.lastPathComponent): \(error.localizedDescription)")
        }
    }
}

// 索引（合并增量：追加模式，保留历史行）
if !indexLines.isEmpty {
    // ★ 必须先确保文件存在：FileHandle(forWritingTo:) 在文件不存在时直接失败，
    //   原来的 try? 会静默吞掉错误，导致索引永远不落盘
    if !fm.fileExists(atPath: indexURL.path) {
        fm.createFile(atPath: indexURL.path, contents: nil)
    }
    if let handle = try? FileHandle(forWritingTo: indexURL) {
        handle.seekToEndOfFile()
        for l in indexLines { handle.write((l + "\n").data(using: .utf8)!) }
        handle.closeFile()
        print("\n索引已追加: \(indexURL.path)  (\(indexLines.count) 行)")
    } else {
        print("\n⚠️  索引写入失败: \(indexURL.path)")
    }
}

print("\n完成: 识别 \(done) / 跳过 \(skipped) / 失败 \(failed) / 共 \(totalChars) 字 / "
      + String(format: "%.0f", Date().timeIntervalSince(startTime)) + "s")
