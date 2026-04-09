# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in HappyFigure, please report it responsibly:

1. **Do not** open a public GitHub issue
2. Email **happyfigure-security@googlegroups.com** with a description of the vulnerability and steps to reproduce
3. Include your name/handle if you'd like to be credited in the fix

We will acknowledge receipt within 48 hours and aim to provide a fix or mitigation within 7 days for critical issues.

## Security Considerations

- **LLM-generated code execution**: The `plot` and `diagram` pipelines generate Python code (matplotlib scripts, data processing) and execute it on the host machine. This is by design for scientific figure generation, but means LLM outputs run with the same permissions as the user. Run HappyFigure in an isolated environment (container, VM, or dedicated user) if processing untrusted input.
- **Data sent to third-party APIs**: HappyFigure sends proposal text, experiment data, and images to third-party LLM APIs (Azure OpenAI, Google Gemini, Anthropic, AWS Bedrock). Review your input data before running. Do not use HappyFigure with confidential or sensitive data without understanding your provider's data handling policies.
- **API key storage**: API keys should be stored in `.env` files (gitignored) or environment variables, never committed to the repository.
- **Microservices**: The microservices (SAM3, OCR, BEN2) run on localhost by default and are not hardened for public exposure. Do not expose service ports to the internet.
