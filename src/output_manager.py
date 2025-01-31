import asyncio
import os
import wave
import logging
import time
import shutil
import re
import sys
import unicodedata
import src.utils as utils
from src.characters_manager import Characters
from src.character_manager import Character
from src.llm.messages import assistant_message, message
from src.llm.message_thread import message_thread
from src.llm.openai_client import openai_client
from src.tts import Synthesizer

class ChatManager:
    def __init__(self, game_state_manager, config, tts: Synthesizer, client: openai_client):
        self.loglevel = 28
        self.game_state_manager = game_state_manager
        self.mod_folder = config.mod_path
        self.max_response_sentences = config.max_response_sentences
        self.language = config.language
        self.add_voicelines_to_all_voice_folders = config.add_voicelines_to_all_voice_folders
        self.offended_npc_response = config.offended_npc_response
        self.forgiven_npc_response = config.forgiven_npc_response
        self.follow_npc_response = config.follow_npc_response
        self.wait_time_buffer = config.wait_time_buffer
        self.__tts: Synthesizer = tts
        self.__client: openai_client = client

        self.character_num = 0
        self.active_character = None
        self.player_name = config.player_name
        self.number_words_tts = config.number_words_tts

        self.wav_file = f'MantellaDi_MantellaDialogu_00001D8B_1.wav'
        self.lip_file = f'MantellaDi_MantellaDialogu_00001D8B_1.lip'

        self.end_of_sentence_chars = ['.', '?', '!', ':', ';']
        self.end_of_sentence_chars = [unicodedata.normalize('NFKC', char) for char in self.end_of_sentence_chars]

        self.sentence_queue = asyncio.Queue()

    def play_sentence_ingame(self, sentence: str, character_to_talk: Character):
        audio_file = self.__tts.synthesize(character_to_talk.voice_model, sentence)
        self.save_files_to_voice_folders([audio_file, sentence])

    def num_tokens(self, content_to_measure: message | str | message_thread | list[message]) -> int:
        if isinstance(content_to_measure, message_thread) or isinstance(content_to_measure, list):
            return openai_client.num_tokens_from_messages(content_to_measure)
        else:
            return openai_client.num_tokens_from_message(content_to_measure, None)
        
    async def get_response(self, messages: message_thread, characters: Characters, radiant_dialogue: bool) -> message_thread:
        sentence_queue: asyncio.Queue[tuple[str,str] | None] = asyncio.Queue()
        event: asyncio.Event = asyncio.Event()
        event.set()

        results = await asyncio.gather(
            self.process_response(sentence_queue, messages, characters, radiant_dialogue, event), 
            self.send_response(sentence_queue, event)
        )
        messages, _ = results

        return messages

    async def get_audio_duration(self, audio_file):
        """Check if the external software has finished playing the audio file"""

        with wave.open(audio_file, 'r') as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()

        # wait `buffer` seconds longer to let processes finish running correctly
        duration = frames / float(rate) + self.wait_time_buffer
        return duration
    

    def setup_voiceline_save_location(self, in_game_voice_folder):
        """Save voice model folder to Mantella Spell if it does not already exist"""
        self.in_game_voice_model = in_game_voice_folder

        in_game_voice_folder_path = f"{self.mod_folder}/{in_game_voice_folder}/"
        if not os.path.exists(in_game_voice_folder_path):
            os.mkdir(in_game_voice_folder_path)

            # copy voicelines from one voice folder to this new voice folder
            # this step is needed for Skyrim to acknowledge the folder
            example_folder = f"{self.mod_folder}/MaleNord/"
            for file_name in os.listdir(example_folder):
                source_file_path = os.path.join(example_folder, file_name)

                if os.path.isfile(source_file_path):
                    shutil.copy(source_file_path, in_game_voice_folder_path)

            self.game_state_manager.write_game_info('_mantella_status', 'Error with Mantella.exe. Please check MantellaSoftware/logging.log')
            logging.warn("Unknown NPC detected. This NPC will be able to speak once you restart Skyrim. To learn how to add memory, a background, and a voice model of your choosing to this NPC, see here: https://github.com/art-from-the-machine/Mantella#adding-modded-npcs")
            time.sleep(5)
            return True
        return False


    @utils.time_it
    def save_files_to_voice_folders(self, queue_output):
        """Save voicelines and subtitles to the correct game folders"""

        audio_file, subtitle = queue_output
        if self.add_voicelines_to_all_voice_folders == '1':
            for sub_folder in os.scandir(self.mod_folder):
                if not sub_folder.is_dir():
                    continue

                shutil.copyfile(audio_file, f"{sub_folder.path}/{self.wav_file}")

                # Copy FaceFX generated LIP file
                try:
                    shutil.copyfile(audio_file.replace(".wav", ".lip"), f"{sub_folder.path}/{self.lip_file}")
                except Exception as e:
                    # only warn on failure
                    logging.warning(e)
        else:
            shutil.copyfile(audio_file, f"{self.mod_folder}/{self.active_character.in_game_voice_model}/{self.wav_file}")

            # Copy FaceFX generated LIP file
            try:
                shutil.copyfile(audio_file.replace(".wav", ".lip"), f"{self.mod_folder}/{self.active_character.in_game_voice_model}/{self.lip_file}")
            except Exception as e:
                # only warn on failure
                logging.warning(e)


        logging.info(f"{self.active_character.name} (character {self.character_num}) should speak")
        if self.character_num == 0:
            self.game_state_manager.write_game_info('_mantella_say_line', subtitle.strip())
        else:
            say_line_file = '_mantella_say_line_'+str(self.character_num+1)
            self.game_state_manager.write_game_info(say_line_file, subtitle.strip())

    @utils.time_it
    def remove_files_from_voice_folders(self):
        for sub_folder in os.listdir(self.mod_folder):
            try:
                os.remove(f"{self.mod_folder}/{sub_folder}/{self.wav_file}")
                os.remove(f"{self.mod_folder}/{sub_folder}/{self.lip_file}")
            except:
                continue


    async def send_audio_to_external_software(self, queue_output):
        logging.debug(f"Dialogue to play: {queue_output[0]}")
        self.save_files_to_voice_folders(queue_output)
        
        
        # Remove the played audio file
        #os.remove(audio_file)

        # Remove the played audio file
        #os.remove(audio_file)

    async def send_response(self, sentence_queue: asyncio.Queue[tuple[str,str]|None], event: asyncio.Event):
        """Send response from sentence queue generated by `process_response()`"""

        while True:
            queue_output = await sentence_queue.get()
            if queue_output is None:
                logging.info('End of sentences')
                break

            # send the audio file to the external software and wait for it to finish playing
            await self.send_audio_to_external_software(queue_output)
            event.set()

            audio_duration = await self.get_audio_duration(queue_output[0])
            # wait for the audio playback to complete before getting the next file
            logging.info(f"Waiting {int(round(audio_duration,4))} seconds...")
            await asyncio.sleep(audio_duration)

    def clean_sentence(self, sentence):
        def remove_as_a(sentence):
            """Remove 'As an XYZ,' from beginning of sentence"""
            if sentence.startswith('As a'):
                if ', ' in sentence:
                    logging.info(f"Removed '{sentence.split(', ')[0]} from response")
                    sentence = sentence.replace(sentence.split(', ')[0]+', ', '')
            return sentence
        
        def parse_asterisks_brackets(sentence):
            if ('*' in sentence):
                # Check if sentence contains two asterisks
                asterisk_check = re.search(r"(?<!\*)\*(?!\*)[^*]*\*(?!\*)", sentence)
                if asterisk_check:
                    logging.info(f"Removed asterisks text from response: {sentence}")
                    # Remove text between two asterisks
                    sentence = re.sub(r"(?<!\*)\*(?!\*)[^*]*\*(?!\*)", "", sentence)
                else:
                    logging.info(f"Removed response containing single asterisks: {sentence}")
                    sentence = ''

            if ('(' in sentence) or (')' in sentence):
                # Check if sentence contains two brackets
                bracket_check = re.search(r"\(.*\)", sentence)
                if bracket_check:
                    logging.info(f"Removed brackets text from response: {sentence}")
                    # Remove text between brackets
                    sentence = re.sub(r"\(.*?\)", "", sentence)
                else:
                    logging.info(f"Removed response containing single bracket: {sentence}")
                    sentence = ''

            return sentence
        
        if ('Well, well, well' in sentence):
            sentence = sentence.replace('Well, well, well', 'Well well well')

        sentence = remove_as_a(sentence)
        sentence = sentence.replace('"','')
        sentence = sentence.replace('[', '(')
        sentence = sentence.replace(']', ')')
        sentence = sentence.replace('{', '(')
        sentence = sentence.replace('}', ')')
        # local models sometimes get the idea in their head to use double asterisks **like this** in sentences instead of single
        # this converts double asterisks to single so that they can be filtered out appropriately
        sentence = sentence.replace('**','*')
        sentence = parse_asterisks_brackets(sentence)
        return sentence


    async def process_response(self, sentence_queue: asyncio.Queue[tuple[str,str] |None], messages : message_thread, characters: Characters, radiant_dialogue: bool, event:asyncio.Event) -> message_thread:
        """Stream response from LLM one sentence at a time"""

        sentence = ''
        remaining_content = ''
        full_reply = ''
        num_sentences = 0
        cumulative_sentence_bool = False
        #Added from xTTS implementation
        accumulated_sentence = ''
        
        while True:
            try:
                start_time = time.time()
                async for content in self.__client.streaming_call(messages= messages):
                    if content is not None:
                        sentence += content
                        # Check for the last occurrence of sentence-ending punctuation
                        punctuations = ['.', '!', ':', '?']
                        last_punctuation = max(sentence.rfind(p) for p in punctuations)
                        if last_punctuation != -1:
                            # Split the sentence at the last punctuation mark
                            remaining_content = sentence[last_punctuation + 1:]
                            current_sentence = sentence[:last_punctuation + 1]
                            
                            # New logic to handle conditions based on the presence of a colon and the state of `accumulated_sentence`
                            content_edit = unicodedata.normalize('NFKC', current_sentence)
                            if ':' in content_edit:
                                if accumulated_sentence:  # accumulated_sentence is not empty
                                    cumulative_sentence_bool = True
                                    
                                else:  # accumulated_sentence is empty
                                    # Split the sentence at the colon
                                    parts = content_edit.split(':', 1)
                                    keyword_extraction = parts[0].strip()
                                    current_sentence = parts[1].strip() if len(parts) > 1 else ''
    
                                    # if LLM is switching character
                                    # Find the first character whose name starts with keyword_extraction
                                    matching_character_key = next((key for key in characters.get_all_names() if key.startswith(keyword_extraction)), None)
                                    if matching_character_key:
                                        logging.info(f"Switched to {matching_character_key}")
                                        self.active_character = characters.get_character_by_name(matching_character_key)
                                        self.__tts.change_voice(self.active_character.voice_model)

                                        # Find the index of the matching character
                                        self.character_num = characters.get_all_names().index(matching_character_key)

                                    elif keyword_extraction == self.player_name:
                                        logging.info(f"Stopped LLM from speaking on behalf of the player")
                                        break
                                    elif keyword_extraction.lower() == self.offended_npc_response.lower():
                                        logging.info(f"The player offended the NPC")
                                        self.game_state_manager.write_game_info('_mantella_aggro', '1')
                                        self.active_character.is_in_combat = 1
                                        
                                    elif keyword_extraction.lower() == self.forgiven_npc_response.lower():
                                        logging.info(f"The player made up with the NPC")
                                        self.game_state_manager.write_game_info('_mantella_aggro', '0')
                                        self.active_character.is_in_combat = 0

                                    elif keyword_extraction.lower() == self.follow_npc_response.lower():
                                        logging.info(f"The NPC is willing to follow the player")
                                        self.game_state_manager.write_game_info('_mantella_aggro', '2')
             
                            if ('assist' in content) and (num_sentences>0):
                                logging.info(f"'assist' keyword found. Ignoring sentence which begins with: {sentence}")
                                break
                            
                            # Accumulate sentences if less than X words
                            if len(accumulated_sentence.split()) < self.number_words_tts and cumulative_sentence_bool == False:
                                accumulated_sentence += current_sentence
                                sentence = remaining_content
                                continue
                            else:
                                if cumulative_sentence_bool == True :
                                    sentence = accumulated_sentence
                                else :
                                    sentence = accumulated_sentence + current_sentence
                                accumulated_sentence = ''
                                if len(sentence.strip()) < 3:
                                    logging.info(f'Skipping voiceline that is too short: {sentence}')
                                    break

                                logging.log(self.loglevel, f"LLM returned sentence took {time.time() - start_time} seconds to execute")

                                if self.active_character :
                                    # Generate the audio and return the audio file path
                                    try:
                                        audio_file = self.__tts.synthesize(self.active_character.voice_model, ' ' + sentence + ' ', self.active_character.is_in_combat)
                                    except Exception as e:
                                        logging.error(f"xVASynth Error: {e}")

                                    # Put the audio file path in the sentence_queue
                                    await sentence_queue.put([audio_file, sentence])

                                    full_reply += sentence
                                    num_sentences += 1
                                    if cumulative_sentence_bool == True :
                                        sentence = current_sentence + remaining_content
                                        cumulative_sentence_bool = False
                                    else :
                                        sentence = remaining_content
                                    remaining_content = ''

                                    # clear the event for the next iteration
                                    event.clear()
                                    # wait for the event to be set before generating the next line
                                    await event.wait()

                                    end_conversation = self.game_state_manager.load_data_when_available('_mantella_end_conversation', '')
                                    radiant_dialogue_update = self.game_state_manager.load_data_when_available('_mantella_radiant_dialogue', '')
                                    # stop processing LLM response if:
                                    # max_response_sentences reached (and the conversation isn't radiant)
                                    # conversation has switched from radiant to multi NPC (this allows the player to "interrupt" radiant dialogue and include themselves in the conversation)
                                    # the conversation has ended
                                    if ((num_sentences >= self.max_response_sentences) and (radiant_dialogue == 'false')) or ((radiant_dialogue == 'true') and (radiant_dialogue_update.lower() == 'false')) or (end_conversation.lower() == 'true'):
                                        break
                break
            except Exception as e:
                logging.error(f"LLM API Error: {e}")
                error_response = "I can't find the right words at the moment."
                self.play_sentence_ingame(error_response, self.active_character)
                # audio_file = self.__tts.synthesize(self.active_character.voice_model, None, error_response)
                # self.save_files_to_voice_folders([audio_file, error_response])
                logging.log(self.loglevel, 'Retrying connection to API...')
                time.sleep(5)

        #Added from xTTS implementation
        # Check if there is any accumulated sentence at the end
        if accumulated_sentence:
            # Generate the audio and return the audio file path
            try:
                #Added from xTTS implementation
                audio_file = self.__tts.synthesize(self.active_character.voice_model, ' ' + accumulated_sentence + ' ', self.active_character.is_in_combat)
                await sentence_queue.put([audio_file, accumulated_sentence])
                full_reply += accumulated_sentence
                accumulated_sentence = ''
                # clear the event for the next iteration
                event.clear()
                # wait for the event to be set before generating the next line
                await event.wait()
                end_conversation = self.game_state_manager.load_data_when_available('_mantella_end_conversation', '')
                radiant_dialogue_update = self.game_state_manager.load_data_when_available('_mantella_radiant_dialogue', '')
            except Exception as e:
                accumulated_sentence = ''
                logging.error(f"xVASynth Error: {e}")
        else:
            logging.info(f"accumulated_sentence at the end is None")
        # Mark the end of the response
        await sentence_queue.put(None)

        messages.add_message(assistant_message(full_reply, characters.get_all_names()))
        logging.log(23, f"Full response saved ({self.__client.calculate_tokens_from_text(full_reply)} tokens): {full_reply}")

        return messages
