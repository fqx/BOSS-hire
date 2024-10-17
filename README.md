# Resume Screening Assistant

This project is an automated resume screening tool that uses OpenAI's GPT models to evaluate candidate profiles based on job requirements.

## Features

- Automated login to a job platform
- Retrieval of job requirements
- Scanning and evaluation of recommended candidates
- Integration with OpenAI's GPT for resume analysis

## Prerequisites

- Python 3.6+
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

## How it works

1. The script launches an undetected Chrome browser and navigates to the specified URL.
2. It logs into the job platform using credentials (make sure to implement this securely).
3. Job requirements are extracted from the provided source.
4. The script then loops through recommended candidates, evaluating each profile against the job requirements using OpenAI's GPT model.
5. Results are logged and can be further processed as needed.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the [MIT License](LICENSE).

## Disclaimer

This tool is for educational purposes only. Make sure to comply with the terms of service of any platforms you interact with and respect privacy laws and regulations.