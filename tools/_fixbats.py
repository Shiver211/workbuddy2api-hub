"""将仓库现有 Windows 批处理脚本规范为 ASCII + CRLF，不重建脚本内容。"""
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    for path in sorted(root.glob("*.bat")):
        text = path.read_bytes().decode("ascii").replace("\r\n", "\n")
        path.write_bytes(text.replace("\n", "\r\n").encode("ascii"))
        print("normalized", path.name)


if __name__ == "__main__":
    main()
