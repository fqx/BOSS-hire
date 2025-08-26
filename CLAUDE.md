# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python-based resume screening automation tool that uses OpenAI's GPT models to evaluate candidate profiles against job requirements. The system automates login to job platforms, extracts job requirements, and screens recommended candidates using AI-powered analysis.

## Architecture

The codebase is organized into modular utility files:

- **main.py** - Entry point that orchestrates the entire workflow: launches webdriver, processes job configurations, coordinates candidate screening
- **driver_utils.py** - Selenium WebDriver utilities for browser automation: login, navigation, UI interactions with job platform
- **job_utils.py** - Job processing logic: candidate evaluation loop, requirement matching, statistics tracking
- **llm_utils.py** - OpenAI integration with structured evaluation system: candidate qualification assessment using Pydantic models
- **log_utils.py** - Custom logging system with tqdm integration and multiple output handlers (console + file)

## Common Commands

### Running the Application
```bash
# Run with default config (params.json)
python main.py

# Run with custom config file
python main.py -c path/to/config.json
```

### Installation and Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Environment setup (create .env file)
OPENAI_API_KEY=your_openai_api_key
OPENAI_BASE_URL=your_openai_base_url  # optional
LOG_LEVEL=INFO
```

### Testing Syntax
```bash
# Check Python syntax across all files
find . -name "*.py" -exec python -m py_compile {} \;
```

## Configuration

Job requirements are configured via JSON files (params.json or custom config):
- **job_title**: Position name for platform search
- **max_idx**: Maximum number of candidates to process  
- **job_requirements**: Filtering criteria including age bounds, required keywords, and detailed requirements
- **url**: Target job platform URL

Multiple job configurations can be processed in a single run by providing an array of job objects.

## Key Dependencies

- **selenium + undetected-chromedriver**: Web automation for job platform interaction
- **openai**: AI-powered candidate evaluation 
- **pydantic**: Structured data validation for evaluation responses
- **tqdm**: Progress tracking with custom logging integration
- **python-dotenv**: Environment variable management

## Logging

The system uses a custom logging setup with:
- Console output via tqdm-compatible handler
- File output to `boss_hire.log` for warnings and errors
- Custom LLM log level (35) for AI evaluation results
- Statistics tracking and final reporting on exit