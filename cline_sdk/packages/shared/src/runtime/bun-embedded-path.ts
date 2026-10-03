/**
 * 模块路径是否指向编译后的 Bun 可执行文件的嵌入式
 * 虚拟文件系统：POSIX 上为 `/$bunfs/root/...`，
 * Windows 上为 `B:\~BUN\root\...`。
 */
export function isBunEmbeddedModulePath(
	modulePath: string | undefined,
): boolean {
	const trimmed = modulePath?.trim();
	if (!trimmed) {
		return false;
	}
	if (trimmed.startsWith("/$bunfs/")) {
		return true;
	}
	const normalized = trimmed.replace(/\\/g, "/").toLowerCase();
	return normalized.startsWith("b:/~bun/");
}
