"""Chinese display messages for Eagle facts; diagnostics remain untouched."""

from danbooru_multi_search.app.eagle_service import EagleResult

ERROR_TEXT = {
    "unsupported_eagle_version": "Eagle 版本过低，需要 4.0 Build 21 或更新版本。",
    "eagle_connection_error": "无法连接本机 Eagle，请确认 Eagle 已启动。",
    "eagle_timeout": "Eagle 请求超时，请稍后手动重试。",
    "eagle_no_library": "Eagle 尚未打开素材库。",
    "eagle_folder_limit": "文件夹超过本次读取范围（20页或20000项）。",
    "eagle_library_changed": "当前素材库已经改变，请刷新后重新选择。",
    "eagle_folder_not_found": "文件夹已改变，请刷新后确认。",
    "eagle_cancelled": "操作已取消。",
    "eagle_queue_rejected": "Eagle 请求无法加入队列，请稍后重试。",
}


def result_message(result: EagleResult) -> str:
    if result.uncertain:
        return "新建请求已经发送，结果无法确认；请刷新文件夹后查看，不要直接重复新建。"
    if result.error_code == "eagle_cancelled" and result.created_identifier:
        return "文件夹已创建，本次操作已取消；请刷新查看，取消不会撤销创建。"
    if result.error_code:
        return ERROR_TEXT.get(result.error_code, "Eagle 请求失败，请查看具体原因。")
    if result.created_identifier:
        return "文件夹已创建；请在列表明确选择目标。"
    return "已连接。请选择本次采集目标文件夹。"
