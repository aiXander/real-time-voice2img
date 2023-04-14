import argparse
import io
import os
import speech_recognition as sr
import whisper
import torch
import time
import asyncio
import httpx

from datetime import datetime, timedelta
from queue import Queue
from tempfile import NamedTemporaryFile
from time import sleep
from sys import platform
from datetime import datetime

from chatgpt_prompting import get_chatgpt_prompt
import settings

def save_string_to_file(text, filename):
    with open(filename, 'w') as file:
        file.write(text)


async def post_request_async(server_url, prompt, outdir, timeout = 100):
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{server_url}/generate", data={"prompt": prompt})

        # Save the image to a file
        with open('result.jpg', 'wb') as f:
            f.write(response.content)

        timestring = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
        with open(f'{outdir}/{timestring}.jpg', 'wb') as f:
            f.write(response.content)

        save_string_to_file(prompt, f'{outdir}/{timestring}.txt')

def get_prompt_from_transcription(transcription, 
                                    mode = "chat_gpt", # "moving_buffer" or "last_line"
                                    verbose=False):

    if verbose:
        # Clear the console to reprint the entire transcription.
        os.system('cls' if os.name=='nt' else 'clear')
        print("Full transcription:")
        for line in transcription:
            print(line)

    if mode == "moving_buffer" or mode == "chat_gpt":
        most_recent_section = ""
        for line in transcription:
            most_recent_section += f" {line}"

        if len(most_recent_section) > settings.section_length:
            most_recent_section = most_recent_section[-(settings.section_length+1):]

        if mode == "chat_gpt":
            print("-----------------------------------------------------")
            print("---------- Most recent transcript section: ----------")
            print(most_recent_section)
            prompt = get_chatgpt_prompt(most_recent_section)

        elif mode == "moving_buffer":
            prompt = most_recent_section

    elif mode == "last_line":
        prompt = transcription[-1]
    else:
        raise ValueError(f"Invalid prompt mode: {mode}")

    return prompt

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="medium", help="Model to use",
                        choices=["tiny", "base", "small", "medium", "large"])
    parser.add_argument("--non_english", action='store_true',
                        help="Don't use the english model.")
    parser.add_argument("--energy_threshold", default=300,
                        help="Energy level for mic to detect.", type=int)
    parser.add_argument("--record_timeout", default=2,
                        help="How real time the recording is in seconds.", type=float)
    parser.add_argument("--phrase_timeout", default=2,
                        help="How much empty space between recordings before we "
                             "consider it a new line in the transcription.", type=float)  
    parser.add_argument("--outdir", default="outputs",
                        help="Ouput directory for all prompts and images.")
    parser.add_argument("--server_url", default="http://localhost:5000",
                        help="Server domain url.")
    if 'linux' in platform:
        parser.add_argument("--default_microphone", default='pulse',
                            help="Default microphone name for SpeechRecognition. "
                                 "Run this with 'list' to view available Microphones.", type=str)
    args = parser.parse_args()

    current_time = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
    args.outdir = os.path.join(args.outdir, current_time)

    # create outdir
    os.makedirs(args.outdir,exist_ok=True)
    # The last time a recording was retreived from the queue.
    phrase_time = None
    # Current raw audio bytes.
    last_sample = bytes()
    # Thread safe Queue for passing data from the threaded recording callback.
    data_queue = Queue()
    # We use SpeechRecognizer to record our audio because it has a nice feauture where it can detect when speech ends.
    recorder = sr.Recognizer()
    recorder.energy_threshold = args.energy_threshold
    # Definitely do this, dynamic energy compensation lowers the energy threshold dramtically to a point where the SpeechRecognizer never stops recording.
    recorder.dynamic_energy_threshold = True
    
    # Important for linux users. 
    # Prevents permanent application hang and crash by using the wrong Microphone
    if 'linux' in platform:
        mic_name = args.default_microphone
        if not mic_name or mic_name == 'list':
            print("Available microphone devices are: ")
            for index, name in enumerate(sr.Microphone.list_microphone_names()):
                print(f"Microphone with name \"{name}\" found")   
            return
        else:
            for index, name in enumerate(sr.Microphone.list_microphone_names()):
                if mic_name in name:
                    source = sr.Microphone(sample_rate=16000, device_index=index)
                    break
    else:
        source = sr.Microphone(sample_rate=16000)
        
    # Load / Download model
    model = args.model
    if args.model != "large" and not args.non_english:
        model = model + ".en"
    audio_model = whisper.load_model(model)

    record_timeout = args.record_timeout
    phrase_timeout = args.phrase_timeout

    temp_file = NamedTemporaryFile().name
    transcription = ['']
    prompt = ""
    
    with source:
        recorder.adjust_for_ambient_noise(source)

    def record_callback(_, audio:sr.AudioData) -> None:
        """
        Threaded callback function to recieve audio data when recordings finish.
        audio: An AudioData containing the recorded bytes.
        """
        # Grab the raw bytes and push it into the thread safe queue.
        data = audio.get_raw_data()
        data_queue.put(data)

    # Create a background thread that will pass us raw audio bytes.
    # We could do this manually but SpeechRecognizer provides a nice helper.
    recorder.listen_in_background(source, record_callback, phrase_time_limit=record_timeout)

    # Cue the user that we're ready to go.
    print("Listening...")

    last_transcription_time = time.time()

    while True:
        try:
            now = datetime.utcnow()
            # Pull raw recorded audio from the queue.
            if not data_queue.empty() and (time.time() - last_transcription_time) > settings.transcribe_every_n_seconds:
                last_sample = bytes()
                while not data_queue.empty():
                    data = data_queue.get()
                    last_sample += data

                # Use AudioData to convert the raw data to wav data.
                audio_data = sr.AudioData(last_sample, source.SAMPLE_RATE, source.SAMPLE_WIDTH)
                wav_data = io.BytesIO(audio_data.get_wav_data())
            
                # Write wav data to the temporary file as bytes.
                with open(temp_file, 'w+b') as f:
                    f.write(wav_data.read())

                # Read the transcription.
                result = audio_model.transcribe(temp_file, fp16=torch.cuda.is_available())
                last_transcription_time = time.time()

                text = result['text'].strip()
                transcription.append(text)

                prompt = get_prompt_from_transcription(transcription, mode = settings.prompt_mode)

                if len(prompt) > 2 and prompt != "Thank you.":
                    print("\n--- Rendering prompt: ", prompt)
                    asyncio.run(post_request_async(args.server_url, prompt, args.outdir))

                    # save the entire transcription to outputs/transcription.txt:
                    with open(f'{args.outdir}/transcription.txt', 'w') as f:
                        for line in transcription:
                            f.write(line + '\n')

                    sleep(0.1)

        except KeyboardInterrupt:
            break

    print("\n\nFinal transcription:")
    for line in transcription:
        print(line)


if __name__ == "__main__":
    main()