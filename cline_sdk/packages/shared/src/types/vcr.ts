/** 单次录制的 HTTP 交互。 */
export interface VcrRecording {
	scope: string;
	method: string;
	path: string;
	body?: string;
	/** 脱敏后的规范请求体，用作可选的回放契约。 */
	requestBody?: string;
	status: number;
	response: unknown;
	responseIsBinary: boolean;
	/** 来自原始响应的 Content-Type 头（在录制时捕获）。 */
	contentType?: string;
}
