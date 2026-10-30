# Resume Screening Assistant

This project is an automated resume screening tool that uses OpenAI's GPT models to evaluate candidate profiles based on job requirements.

## Features

- Automated login to a job platform
- Retrieval of job requirements
- Scanning and evaluation of recommended candidates
- Integration with OpenAI's GPT for resume analysis

## Prerequisites

- Python 3.10+
- Chrome browser

## Installation

1. Clone the repository:

2. Install required packages:

3. Set up environment variables:
   Create a `.env` file in the project root and add the following:
   ```
   OPENAI_API_KEY=your_openai_api_key
   OPENAI_BASE_URL=your_openai_base_url (optional)
   LOG_LEVEL=INFO
   ```

## Usage

1. Prepare a configuration file (e.g., `params.json`)

2. Run the script:
   ```
   python main.py -c path/to/your/config.json
   ```
   If no config file is specified, it will use `params.json` by default.

### 简历关键词条件

`job_requirements.cv_required_keywords` 支持列表和树状配置，按简历卡片文本进行区分大小写的子串匹配。

原有列表保持“任意一个命中即可”的规则，空列表表示不限制关键词：

```json
"cv_required_keywords": ["A", "B", "C"]
```

必须包含 A，并且 B、C、D 至少包含一个：

```json
"cv_required_keywords": {
  "A": ["B", "C", "D"]
}
```

父节点与子条件之间是“且”，同层分支之间是“或”。支持继续嵌套，例如下列条件表示 `(A 且 B 且 (C 或 D)) 或 (E 且 F)`：

```json
"cv_required_keywords": {
  "A": {"B": ["C", "D"]},
  "E": ["F"]
}
```

`{"A": []}` 表示只要求 A。每个关键词须单独写成字符串：`["B", "C", "D"]` 表示三选一，`["BCD"]` 表示匹配完整的 `BCD`。诊断日志仍记录所有命中的关键词，是否通过则按完整树状条件判断。

## How it works

1. The script launches a Chrome browser via [zendriver](https://github.com/cdpdriver/zendriver) (CDP-based, undetectable by anti-bot systems) and navigates to the specified URL.
2. It prompts login via QR code scan and waits for the session to be established.
3. Job requirements are loaded from the config file.
4. The script loops through recommended candidates, evaluating each profile against the job requirements using OpenAI's GPT model.
5. Qualified candidates are greeted automatically. Results are logged to console and `boss_hire.log`.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the [MIT License](LICENSE).

## Disclaimer

This tool is for educational purposes only. Make sure to comply with the terms of service of any platforms you interact with and respect privacy laws and regulations.
