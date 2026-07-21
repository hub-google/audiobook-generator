import asyncio
import edge_tts
import os
import yaml

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

async def generate_ref():
    config = load_config()
    tts_config = config.get('tts', {})
    
    prompt_text = tts_config.get('prompt_text')
    edge_voice = tts_config.get('edge_voice', 'zh-CN-YunxiNeural')
    ref_audio_path_rel = tts_config.get('ref_audio_path')
    
    if not prompt_text or not ref_audio_path_rel:
        print("[Error] prompt_text or ref_audio_path is missing in config.yaml.")
        return

    # 計算儲存路徑
    root_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    ref_audio_path = os.path.abspath(os.path.join(root_dir, ref_audio_path_rel))
    ref_dir = os.path.dirname(ref_audio_path)
    
    if not os.path.exists(ref_dir):
        os.makedirs(ref_dir)

    print(f"Generating reference audio using voice: {edge_voice}...")
    comm = edge_tts.Communicate(prompt_text, edge_voice)
    await comm.save(ref_audio_path)
    
    # Write the prompt text to a txt file with the same name for reference
    ref_txt_path = os.path.splitext(ref_audio_path)[0] + ".txt"
    with open(ref_txt_path, "w", encoding="utf-8") as f:
        f.write(prompt_text)
        
    print(f"Reference audio generation complete.")
    print(f"Audio saved to: {ref_audio_path}")
    print(f"Text saved to: {ref_txt_path}")

if __name__ == "__main__":
    asyncio.run(generate_ref())
