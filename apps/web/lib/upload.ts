/** Upload constraints, mirrored from the API's single source of truth.
 *
 * The backend rejects anything outside `UPLOAD_SUFFIXES` (app/main.py) with a
 * 400 before the file is written, so these values must stay in step with it.
 * `.xls` is intentionally excluded: reading it needs `xlrd`, which the API does
 * not declare.
 */
export const UPLOAD_SUFFIXES = [".csv", ".xlsx"] as const;

export const UPLOAD_ACCEPT_ATTR = UPLOAD_SUFFIXES.join(",");

export const UPLOAD_MAX_MB = 50;

export const UPLOAD_FORMAT_HINT = `支持 CSV、XLSX，可一次选择多个文件（单文件不超过 ${UPLOAD_MAX_MB} MB）`;
