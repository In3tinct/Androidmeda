import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from absl import app,flags
from typing import Sequence
import os
import traceback
import sys
import json
import threading
import asyncio
from collections import defaultdict
from google.api_core.exceptions import ResourceExhausted
import ollama
import openai
import anthropic

_LLM_PROVIDER = flags.DEFINE_string(
    'llm_provider', None, 'LLM Provider to use e.g. google, openai, anthropic, ollama, vllm')
_LLM_MODEL = flags.DEFINE_string(
    'llm_model', None, 'LLM Model to use e.g gemini-2.0-flash, gpt-4.1')
_OUTPUT_DIR = flags.DEFINE_string(
    'output_dir', None, 'the output directory to save the report and source code (if flag provided)')
_SOURCE_DIR = flags.DEFINE_spaceseplist('source_dir', [], 'List of Directory of the Source code')
_SAVE_CODE = flags.DEFINE_boolean(
    'save_code', False, 'If provided we will save the deobfuscated code')
_THREAD_SIZE = flags.DEFINE_integer(
    'thread_size',1, 'No. of threads to use for concurrent requests to Gemini')

async def send_code_to_llm(system_instructions, files_data, llm_client=None):
    complete_prompt = system_instructions + "\n\n" + files_data
    retry_delay = 2  # Initial retry delay in seconds
    max_retries = 5  # Maximum number of retries

    for attempt in range(max_retries):
        try:
            if "google" in _LLM_PROVIDER.value:
                response_template = await llm_client.generate_content_async([complete_prompt,],
                    safety_settings={
                        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                    }
                )
                return response_template.text
            elif "ollama" in _LLM_PROVIDER.value:
                response = ollama.generate(model=_LLM_MODEL.value, format="json", prompt=complete_prompt)
                return response.response
            elif "openai" in _LLM_PROVIDER.value or "vllm" in _LLM_PROVIDER.value:
                chat_completion = llm_client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": system_instructions},
                        {"role": "user", "content": files_data},
                    ],
                    model=_LLM_MODEL.value,
                )
                return chat_completion.choices[0].message.content
            elif "anthropic" in _LLM_PROVIDER.value:
                message = llm_client.messages.create(
                    model=_LLM_MODEL.value,
                    max_tokens=4096,
                    system=system_instructions,
                    messages=[
                        {"role": "user", "content": files_data}
                    ]
                )
                return message.content[0].text
        except ResourceExhausted as e:
            print(f"Rate limit error: {e}")
            print(f"Retrying in {retry_delay} seconds (attempt {attempt + 1}/{max_retries})...")
            await asyncio.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff
        except Exception as e:
            print(f"LLM API Error: {e}")
            traceback.print_exc()
            sys.exit()

output_data_lock = threading.Lock()
output_data = defaultdict(list)

def process_response_vuln(response_text,file_path):
    with output_data_lock:
        #Removing JSON block from LLM response to process
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(response_text)

        # 2. Read the Security Vulnerabilities section
        vulnerabilities = data['Vulnerabilities']
        
        #Only process files where vulns have been identified
        if len(vulnerabilities) > 0:
            output_data[file_path].extend(vulnerabilities)

def process_response_code(response_text,file_path,output_dir):
    with output_data_lock:
        #Removing JSON block from LLM response to process
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(response_text)

        #Read the code between Java code block
        java_code = data['Code']
        create_unobfuscated_code_files(output_dir,file_path,java_code)

def create_unobfuscated_code_files(code_output_directory, file_path, java_code):
    
    # Extract directory path from file path
    directory = os.path.join(code_output_directory,os.path.dirname(file_path))
    # Create directories recursively, if they don't exist
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    # Create the file
    full_file_path = os.path.join(code_output_directory, file_path)
    with open(full_file_path, 'a+',encoding="utf-8") as output_file:
        output_file.write(java_code)

def find_java_files(directory):
    """
    Recursively searches for Java files within the specified directory.

    Args:
        directory: The directory to start the search from.

    Returns:
        A list of paths to all found Java files.
    """
    java_files = []

    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".java"):
                java_files.append(os.path.join(root,file))

    return java_files

def read_file_content(file_path):
    """Reads the content of a file."""
    with open(file_path, 'r',encoding="utf-8") as file:
      content = file.read()+"\n\n"
    return content

async def process_code_files(semaphore, file_path, llm_client):
    
    async with semaphore:
        try:
            print(f"Processing file with LLM - {file_path}")
            
            #Read the code file
            content = read_file_content(file_path)

            # Sending instructions to find vuln
            response = await send_code_to_llm(prompt_vuln, content, llm_client)
            process_response_vuln(response, file_path)
            
            # Sending instructions to deobfuscate code
            if (_SAVE_CODE.value):
                response = await send_code_to_llm(prompt_deobfuscate, content, llm_client)
                process_response_code(response, file_path, _OUTPUT_DIR.value)
        except Exception as e:
            print(f"Error processing file {file_path}: {e}")
            traceback.print_exc()

def write_vuln_output(output_vuln_dir):
    output_json = {}
    
    for file_name, vulnerabilities in output_data.items():
        output_json[file_name] = vulnerabilities
    
    with open(output_vuln_dir, "w+", encoding="utf-8") as output_file:
        json.dump(output_json, output_file, indent=4)
    

async def main(argv: Sequence[str]) -> None:
    flags.FLAGS(argv)
    if _OUTPUT_DIR.value is None or  len(_SOURCE_DIR.value) == 0:
        raise app.UsageError(
            f'Usage: {argv[0]} -llm_model=<LLM Provider> -llm_model=<LLM Model to use> -output_dir=<output directory> -source_dir=<source directory>'
        )
    
    llm_client = None
    api_key = os.environ.get('API_KEY')
    api_base_url = os.environ.get('API_BASE_URL')

    if _LLM_PROVIDER.value is None:
        raise app.UsageError(
                f'Usage: Model provider is required e.g google, anthropic, openai, ollama, vllm'
            )
    elif all(x not in _LLM_PROVIDER.value for x in ("ollama", "vllm")) and api_key is None: # Ollama and vLLM do not require an API key
        raise app.UsageError(
                f'Usage: {_LLM_PROVIDER.value} model requires an API key. Please set the API_KEY environment variable.'
            )
    elif "vllm" in _LLM_PROVIDER.value and api_base_url is None:
        raise app.UsageError(
                f'Usage: {_LLM_PROVIDER.value} model requires an API BASE URL. Please set the API_BASE_URL environment variable.'
            )
    elif _LLM_MODEL.value is None:
        raise app.UsageError(
                f'Usage: Model name is required e.g gemini-1.5-flash, gpt-4.1, llama3.2'
            )
    
    if "google" in _LLM_PROVIDER.value:
        genai.configure(api_key=api_key)
        llm_client = genai.GenerativeModel(_LLM_MODEL.value)
    elif "openai" in _LLM_PROVIDER.value:
        llm_client = openai.OpenAI(api_key=api_key)
    elif "anthropic" in _LLM_PROVIDER.value:
        llm_client = anthropic.Anthropic(api_key=api_key)
    elif "ollama" in _LLM_PROVIDER.value:
        llm_client = None #We don't need to do anything
    elif "vllm" in _LLM_PROVIDER.value:
        llm_client = openai.OpenAI(api_key=api_key, base_url=api_base_url)
    else:
        raise ValueError(f"Unsupported LLM provider: {_LLM_PROVIDER.value}")

    semaphore = asyncio.Semaphore(_THREAD_SIZE.value)
    
    global prompt_vuln
    global prompt_deobfuscate

    #Find all java files within given directories, comma separated directories are accepted.
    code_files = []
    for source_dirs in _SOURCE_DIR.value:
        code_files = code_files + find_java_files(source_dirs)

    #Read the vulnerability prompt
    with open("prompt.txt", 'r',encoding="utf-8") as file:
      prompt_vuln = file.read()

    #Read the deobfuscation prompt
    if (_SAVE_CODE.value):
        with open("prompt_deobfuscate.txt", 'r',encoding="utf-8") as file:
            prompt_deobfuscate = file.read()

    tasks = []
    
    #Parallel processing of files with Gemini
    if (len(code_files) > 0):
        for file_path in code_files:
            task = asyncio.create_task(
            process_code_files(semaphore, file_path, llm_client)
            )
            tasks.append(task)

        await asyncio.gather(*tasks)

        #Save the vulns
        if (len(output_data.items()) > 0):
            # Create output directory, if they don't exist
            if not os.path.exists(_OUTPUT_DIR.value):
                os.makedirs(_OUTPUT_DIR.value, exist_ok=True)
            output_vuln_path = os.path.join(_OUTPUT_DIR.value,"vuln_report")
            write_vuln_output(output_vuln_path)
            print("***** Vulnerability report created at " + output_vuln_path)
        else:
            print("No Vulnerability found to report")

        if (_SAVE_CODE.value):
            print ("***** Final Processed code files created at " + _OUTPUT_DIR.value)
    else:
        print("No java files found")

if __name__ == '__main__':
    asyncio.run(main(sys.argv))
