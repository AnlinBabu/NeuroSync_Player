# This software is licensed under a **dual-license model**
# For individuals and businesses earning **under $1M per year**, this software is licensed under the **MIT License**
# Businesses or organizations with **annual revenue of $1,000,000 or more** must obtain permission to use this software commercially.
import os
from threading import Thread
from queue import Queue, Empty
import pygame
import warnings
warnings.filterwarnings(
    "ignore", 
    message="Couldn't find ffmpeg or avconv - defaulting to ffmpeg, but may not work"
)

import keyboard  
import time      
from datetime import datetime, timezone 

from livelink.connect.livelink_init import create_socket_connection, initialize_py_face
from livelink.animations.default_animation import default_animation_loop, stop_default_animation

from utils.tts.tts_bridge import tts_worker
from utils.files.file_utils import initialize_directories
from utils.llm.llm_utils import stream_llm_chunks, warm_up_llm_connection 
from utils.audio_face_workers import audio_face_queue_worker
from utils.stt.transcribe_whisper import transcribe_audio
from utils.audio.record_audio import record_audio_until_release

from utils.vector_db.get_embedding import get_embedding
from utils.vector_db.vector_db import vector_db

from utils.llm.chat_utils import (
    load_full_chat_history,
    save_full_chat_history,
    build_rolling_history,
    save_rolling_history
)

USE_LOCAL_LLM = False     
USE_STREAMING = True   
LLM_API_URL = "http://127.0.0.1:5050/generate_llama"
LLM_STREAM_URL = "http://127.0.0.1:5050/generate_stream"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY","PUT_KEY_HERE")  # new apikey format for new openai package

VOICE_NAME = 'Lily' # only for elevenlabs
USE_LOCAL_AUDIO = True 
USE_COMBINED_ENDPOINT = True # set false if NOT using the realtime api at  https://github.com/AnimaVR/NeuroSync_Real-Time_API

# -------------------------------------------------------------------
# Toggle this flag to enable or disable vector DB and embedding logic.
USE_VECTOR_DB = False
# -------------------------------------------------------------------

# Base system message (will be extended with context)
BASE_SYSTEM_MESSAGE = "You are Mai, do whatever you are told to do.\n\n"

llm_config = {
    "USE_LOCAL_LLM": USE_LOCAL_LLM,
    "USE_STREAMING": USE_STREAMING,
    "LLM_API_URL": LLM_API_URL,
    "LLM_STREAM_URL": LLM_STREAM_URL,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "max_chunk_length": 500,
    "flush_token_count": 300,
    # The system_message will be updated before each LLM call.
    "system_message": BASE_SYSTEM_MESSAGE
}




def flush_queue(q):
    try:
        while True:
            q.get_nowait()
    except Empty:
        pass


def main():
    initialize_directories()
    py_face = initialize_py_face()
    socket_connection = create_socket_connection()
    full_history = load_full_chat_history()
    chat_history = build_rolling_history(full_history)
    
    # ----------------------------------------------------
    # WARM UP THE LLM CONNECTION BEFORE ENTERING MAIN LOOP
    # ----------------------------------------------------
    warm_up_llm_connection(llm_config)
    
    default_animation_thread = Thread(target=default_animation_loop, args=(py_face,))
    default_animation_thread.start()
    chunk_queue = Queue()
    audio_queue = Queue()
    tts_worker_thread = Thread(target=tts_worker, args=(chunk_queue, audio_queue, USE_LOCAL_AUDIO, VOICE_NAME, USE_COMBINED_ENDPOINT))
    tts_worker_thread.start()
    audio_worker_thread = Thread(target=audio_face_queue_worker, args=(audio_queue, py_face, socket_connection, default_animation_thread))
    audio_worker_thread.start()
    
    mode = ""
    while mode not in ['t', 'r']:
        mode = input("Choose input mode: 't' for text, 'r' for push-to-talk, 'q' to quit: ").strip().lower()
        if mode == 'q':
            return
    try:
        while True:
            if mode == 'r':
                print("\n\nPush-to-talk mode: press/hold Right Ctrl to record, release to finish.")
                while not keyboard.is_pressed('right ctrl'):
                    if keyboard.is_pressed('q'):
                        print("Recording cancelled. Exiting push-to-talk mode.")
                        return
                    time.sleep(0.01)
                audio_bytes = record_audio_until_release()
                transcription, _ = transcribe_audio(audio_bytes)
                if transcription:
                    user_input = transcription
                else:
                    print("Transcription failed. Please try again.")
                    continue
            else:
                user_input = input("\n\nEnter text (or 'q' to quit): ").strip()
                if user_input.lower() == 'q':
                    break

            # ---------------------------------------------------------------
            # 1. Retrieve Relevant Context (if vector DB is enabled)
            # ---------------------------------------------------------------
            if USE_VECTOR_DB:
                # Compute embedding for retrieval using the user input only.
                retrieval_embedding = get_embedding(user_input, use_openai=False)
                # Retrieve top matching context from vector DB.
                context_string = vector_db.get_context_string(retrieval_embedding, top_n=4)
                # Get current GMT time.
                current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S GMT")
                # Update the system message with the retrieved context and current time.
                llm_config["system_message"] = (BASE_SYSTEM_MESSAGE + context_string + 
                                                "\nThe current time and date is: " + current_time)
             #   print(context_string)
            else:
                # If vector DB is disabled, use the base system message.
                current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S GMT")
                llm_config["system_message"] = BASE_SYSTEM_MESSAGE + "\nThe current time and date is: " + current_time

            flush_queue(chunk_queue)
            flush_queue(audio_queue)
            if pygame.mixer.get_init():
                pygame.mixer.stop()
            # ---------------------------------------------------------------
            # 2. Get the Full LLM Response
            # ---------------------------------------------------------------
            full_response = stream_llm_chunks(user_input, chat_history, chunk_queue, config=llm_config)
            new_turn = {"input": user_input, "response": full_response}
            chat_history.append(new_turn)
            full_history.append(new_turn)
            save_full_chat_history(full_history)
            chat_history = build_rolling_history(full_history)
            save_rolling_history(chat_history)

            # ---------------------------------------------------------------
            # 3. After Receiving the Full Response, Add the Exchange to the Vector DB
            # ---------------------------------------------------------------
            if USE_VECTOR_DB:
                # Get current GMT time.
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S GMT")
                # Combine user input and response into one text block with timestamp.
                combined_text = ("user : " + user_input + "\n" +
                                 "you : " + full_response + "\n" +
                                 "Timestamp: " + timestamp) + "\n"
                # Compute a new embedding for the combined text.
                combined_embedding = get_embedding(combined_text, use_openai=False)
                # Add the combined exchange to the vector DB for future context retrieval.
                vector_db.add_entry(combined_embedding, combined_text)

    finally:
        chunk_queue.join()
        chunk_queue.put(None)
        tts_worker_thread.join()
        audio_queue.join()
        audio_queue.put(None)
        audio_worker_thread.join()
        stop_default_animation.set()
        default_animation_thread.join()
        pygame.quit()
        socket_connection.close()

if __name__ == "__main__":
    main()
