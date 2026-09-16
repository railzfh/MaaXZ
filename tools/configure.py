from pathlib import Path

import shutil

assets_dir = Path(__file__).parent.parent.resolve() / "assets"


def configure_ocr_model():
    assets_ocr_dir = assets_dir / "MaaCommonAssets" / "OCR"
    ocr_dir = assets_dir / "resource" / "model" / "ocr"

    # 本项目已移除 MaaCommonAssets 子模块，OCR 模型直接放在 resource/model/ocr/。
    # 若模型已就位（本地开发或 CI 预置），无需任何操作。
    if ocr_dir.exists() and any(ocr_dir.iterdir()):
        print("Found existing OCR directory, skipping default OCR model import.")
        return

    if not assets_ocr_dir.exists():
        print(
            f"OCR model not found at {ocr_dir}, and {assets_ocr_dir} does not exist.\n"
            f"请把 det.onnx / keys.txt / rec.onnx 放到 {ocr_dir}，"
            "或下载 https://download.maafw.xyz/MaaCommonAssets/OCR/ppocr_v6/ppocr_v6-small.zip 解压进去。"
        )
        return

    shutil.copytree(
        assets_ocr_dir / "ppocr_v6" / "small",
        ocr_dir,
        dirs_exist_ok=True,
    )


if __name__ == "__main__":
    configure_ocr_model()

    print("OCR model configured.")
