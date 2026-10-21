class VideoError(Exception):
    """クライアントに開示してよい固定コードと説明のみを保持します。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


class SubmissionUnknown(VideoError):
    def __init__(self):
        super().__init__("submission_unknown", "送信結果を確認できません。重複課金を避けるため自動再送しません。", 502)
