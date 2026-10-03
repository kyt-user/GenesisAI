/**
 * 路径解析辅助函数，容忍 macOS 文件名中常见的 Unicode 变体
 *（尤其是截图时间戳中的 U+202F NARROW NO-BREAK SPACE，
 * 如 "Screenshot 2026-05-12 at 4.42.48\u202FPM.png"）。
 *
 * 当这样的路径经过剪贴板、终端、粘贴解码器
 * 或其他会规范化空白的层时，U+202F 可能折叠为
 * 普通空格（U+0020）。磁盘上的文件名仍包含 U+202F，因此
 * 字面量 `fs.stat` / `fs.readFile` 会以 ENOENT 失败。
 *
 * `resolveExistingFilePath` 尝试一小组有针对性的变体和一个
 * 最后兜底的父目录扫描来恢复实际文件名。
 */
import { existsSync, readdirSync } from "node:fs";
import { basename, dirname, join } from "node:path";

// 出现在 macOS 生成的文件名中（或被剪贴板/终端替换掉）的
// Unicode 空白码点。
const UNICODE_SPACES_RE = /[\u00A0\u2000-\u200A\u202F\u205F\u3000]/g;
const NARROW_NO_BREAK_SPACE = "\u202F";

// 构建 "规范空白" 键，用于将粘贴/输入的路径
// 与磁盘上的实际条目比较，无论段之间
// 使用了哪种特殊空格变体。
function collapseUnicodeWhitespace(name: string): string {
	return name.normalize("NFC").replace(UNICODE_SPACES_RE, " ");
}

function tryMacOSAmPmVariant(filePath: string): string {
	// macOS Sonoma+ 在截图名称的 AM/PM 前插入 U+202F。某些
	// 区域（例如 en_AU）会输出小写 am/pm；因此使用 /i 标志。
	const fileName = basename(filePath);
	const variantName = fileName.replace(
		/ (AM|PM)\./gi,
		`${NARROW_NO_BREAK_SPACE}$1.`,
	);
	return variantName === fileName
		? filePath
		: join(dirname(filePath), variantName);
}

function tryNFDVariant(filePath: string): string {
	// HFS+ / 某些 APFS 配置以分解形式存储文件名。用户
	// 通常输入或粘贴 NFC。当字面量
	// 路径无法解析时尝试 NFD 变体。
	return filePath.normalize("NFD");
}

function tryCurlyApostropheVariant(filePath: string): string {
	// macOS 在本地化的截图名称（如 "Capture d'écran"）中
	// 使用 U+2019（右单引号）。用户
	// 通常输入 U+0027。
	return filePath.replace(/'/g, "\u2019");
}

function scanDirForCanonicalMatch(filePath: string): string | undefined {
	// 最后兜底：枚举父目录，寻找
	// 规范空白形式与请求的基名匹配的条目。
	// 捕获上面的针对性变体未覆盖的任意特殊空格
	// 不匹配。
	const dir = dirname(filePath);
	const wanted = collapseUnicodeWhitespace(basename(filePath));
	try {
		for (const entry of readdirSync(dir)) {
			if (collapseUnicodeWhitespace(entry) === wanted) {
				return join(dir, entry);
			}
		}
	} catch {
		// 目录不可读或不存在 -- 没有可回退的内容。
	}
	return undefined;
}

/**
 * 将可能被损坏的文件路径解析为磁盘上的实际条目。
 *
 * 当路径已存在时返回字面量路径；否则尝试一小组
 * macOS 特有变体（AM/PM 前的窄不换行空格、NFD
 * 规范化、弯引号），然后回退到折叠特殊 Unicode
 * 空白后比较文件名的父目录扫描。
 *
 * 找不到匹配文件时返回 `undefined`。
 */
export function resolveExistingFilePath(filePath: string): string | undefined {
	if (existsSync(filePath)) {
		return filePath;
	}

	const amPmVariant = tryMacOSAmPmVariant(filePath);
	if (amPmVariant !== filePath && existsSync(amPmVariant)) {
		return amPmVariant;
	}

	const nfdVariant = tryNFDVariant(filePath);
	if (nfdVariant !== filePath && existsSync(nfdVariant)) {
		return nfdVariant;
	}

	const curlyVariant = tryCurlyApostropheVariant(filePath);
	if (curlyVariant !== filePath && existsSync(curlyVariant)) {
		return curlyVariant;
	}

	const nfdCurlyVariant = tryCurlyApostropheVariant(nfdVariant);
	if (
		nfdCurlyVariant !== nfdVariant &&
		nfdCurlyVariant !== curlyVariant &&
		existsSync(nfdCurlyVariant)
	) {
		return nfdCurlyVariant;
	}

	return scanDirForCanonicalMatch(filePath);
}
