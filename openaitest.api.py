import openai
import os

# Set your API key and base URL for vLLM
api_key = os.environ.get('API_KEY')
api_base_url = os.environ.get('API_BASE_URL')

# Initialize OpenAI client pointing to vLLM
client = openai.OpenAI(api_key=api_key, base_url=api_base_url)

# Simple test message
response = client.chat.completions.create(
    model="Qwen3-Coder-480B-A35B-Instruct-FP8",
    messages=[{"role": "user", "content": "hello?"}],
    max_tokens=4096
)

print(response.choices[0].message.content)
