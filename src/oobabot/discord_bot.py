# Purpose: Discord client for Rosie
#

import asyncio
import re
import typing

import discord

from oobabot.decide_to_respond import DecideToRespond
from oobabot.fancy_logging import get_logger
from oobabot.image_generator import ImageGenerator
from oobabot.ooba_client import OobaClient
from oobabot.prompt_generator import PromptGenerator
from oobabot.repetition_tracker import RepetitionTracker
from oobabot.response_stats import AggregateResponseStats
from oobabot.types import ChannelMessage
from oobabot.types import DirectMessage
from oobabot.types import GenericMessage

FORBIDDEN_CHARACTERS = r"[\n\r\t]"
FORBIDDEN_CHARACTERS_PATTERN = re.compile(FORBIDDEN_CHARACTERS)


def sanitize_string(raw_string: str) -> str:
    """
    Filter out any characters that would confuse the AI
    """
    return FORBIDDEN_CHARACTERS_PATTERN.sub(" ", raw_string)


def discord_message_to_generic_message(raw_message: discord.Message) -> GenericMessage:
    """
    Convert a discord message to a GenericMessage or subclass thereof
    """
    generic_args = {
        "author_id": raw_message.author.id,
        "author_name": sanitize_string(raw_message.author.name),
        "message_id": raw_message.id,
        "body_text": sanitize_string(raw_message.content),
        "author_is_bot": raw_message.author.bot,
        "send_timestamp": raw_message.created_at.timestamp(),
    }
    if isinstance(raw_message.channel, discord.DMChannel):
        return DirectMessage(**generic_args)
    if (
        isinstance(raw_message.channel, discord.TextChannel)
        or isinstance(raw_message.channel, discord.GroupChannel)
        or isinstance(raw_message.channel, discord.Thread)
    ):
        return ChannelMessage(
            channel_id=raw_message.channel.id,
            mentions=[mention.id for mention in raw_message.mentions],
            **generic_args,
        )
    get_logger().warning(
        f"Unknown channel type {type(raw_message.channel)}, "
        + f"unsolicited replies disabled.: {raw_message.channel}"
    )
    return GenericMessage(**generic_args)


class DiscordBot(discord.Client):
    # seconds after which we'll lazily purge a channel
    # from channel_last_direct_response

    def __init__(
        self,
        ooba_client: OobaClient,
        decide_to_respond: DecideToRespond,
        prompt_generator: PromptGenerator,
        repetition_tracker: RepetitionTracker,
        aggregate_response_stats: AggregateResponseStats,
        image_generator: ImageGenerator | None,
        ai_name: str,
        persona: str,
        ignore_dms: bool,
        dont_split_responses: bool,
        reply_in_thread: bool,
        log_all_the_things: bool,
    ):
        self.ooba_client = ooba_client
        self.decide_to_respond = decide_to_respond
        self.prompt_generator = prompt_generator
        self.repetition_tracker = repetition_tracker
        self.aggregate_response_stats = aggregate_response_stats

        self.ai_name = ai_name
        self.persona = persona
        self.ai_user_id = -1
        self.image_generator = image_generator

        self.ignore_dms = ignore_dms
        self.dont_split_responses = dont_split_responses
        self.reply_in_thread = reply_in_thread
        self.log_all_the_things = log_all_the_things

        # a list of timestamps in which we last posted to a channel
        self.channel_last_direct_response = {}

        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(intents=intents)

    async def init_commands(self):
        @discord.app_commands.command(
            name="lobotomize",
            description=f"Erase {self.ai_name}'s memory of any message "
            + "before now in this channel.",
        )
        @discord.app_commands.guild_only()
        async def lobotomize(interaction: discord.Interaction):
            async def fail():
                get_logger().warning(
                    "lobotomize called from an unexpected channel: "
                    + f"{interaction.channel_id}"
                )
                await interaction.response.send_message(
                    "failed to lobotomize", ephemeral=True, silent=True
                )

            if interaction.channel_id is None:
                await fail()
                return

            # find the current message in this channel
            # tell the Repetition Tracker to hide messages
            # before this message
            channel = self.get_channel(interaction.channel_id)
            if channel is None:
                await fail()
                return

            if not isinstance(channel, discord.abc.Messageable):
                await fail()
                return

            # find the current message in this channel
            # tell the Repetition Tracker to hide messages
            # before this message
            async for message in channel.history(limit=1):
                get_logger().info(
                    f"lobotomize called in channel {channel.id}, "
                    + f"hiding messages before {message.id}"
                )
                get_logger().info(
                    f"lobotomize called for guid {channel.guild}"
                    + f" # {channel.guild.id}, "
                )
                self.repetition_tracker.hide_messages_before(
                    channel_id=channel.id,
                    message_id=message.id,
                )
            await interaction.response.send_message(
                "Memory wiped!", ephemeral=True, silent=True
            )

        get_logger().debug("Registering commands, this may take a while sometimes...")

        tree = discord.app_commands.CommandTree(self)
        tree.add_command(lobotomize)
        commands = await tree.sync(guild=None)
        for command in commands:
            get_logger().info(
                f"Registered command: {command.name}: {command.description}"
            )
        get_logger().debug(
            "If you try to run any command within the first ~5 minutes of "
            + "the bot starting, it will fail with the error: 'This command "
            + "is outdated, please try again in a few minutes'.  "
            + "This is apparently what Discord just does, and nothing we can "
            + " fix.  Sorry!"
        )

    async def on_ready(self) -> None:
        guilds = self.guilds
        num_guilds = len(guilds)
        num_channels = sum([len(guild.channels) for guild in guilds])

        # todo: re-enable this after more testing
        # try:
        #     # register the commands
        #     await self.init_commands()
        # except Exception as e:
        #     get_logger().warning(
        #         f"Failed to register commands: {e} (continuing without commands)"
        #     )

        if self.user:
            self.ai_user_id = self.user.id
            user_id_str = str(self.ai_user_id)
        else:
            user_id_str = "<unknown>"

        get_logger().info(f"Connected to discord as {self.user} (ID: {user_id_str})")
        get_logger().debug(
            f"monitoring {num_channels} channels across " + f"{num_guilds} server(s)"
        )
        if self.ignore_dms:
            get_logger().debug("Ignoring DMs")
        else:
            get_logger().debug("listening to DMs")

        if self.dont_split_responses:
            get_logger().debug("Responses: returned as single messages")
        else:
            get_logger().debug("Responses: streamed as separate sentences")

        if self.image_generator:
            get_logger().debug("Image generation: enabled")
        else:
            get_logger().debug("Image generation: disabled")

        get_logger().debug(f"AI name: {self.ai_name}")
        get_logger().debug(f"AI persona: {self.persona}")

        get_logger().debug(f"History: {self.prompt_generator.history_lines} lines ")

        str_wakewords = (
            ", ".join(self.decide_to_respond.wakewords)
            if self.decide_to_respond.wakewords
            else "<none>"
        )
        get_logger().debug(f"Wakewords: {str_wakewords}")

    async def on_message(self, raw_message: discord.Message) -> None:
        try:
            message = discord_message_to_generic_message(raw_message)
            should_respond, is_summon = self.decide_to_respond.should_reply_to_message(
                self.ai_user_id, message
            )
            if not should_respond:
                return
            async with raw_message.channel.typing():
                image_prompt = None
                if self.image_generator is not None:
                    # are we creating an image?
                    image_prompt = self.image_generator.maybe_get_image_prompt(
                        raw_message
                    )

                message_task, response_channel = await self.send_response(
                    message=message,
                    raw_message=raw_message,
                    image_requested=image_prompt is not None,
                )
                if response_channel is None:
                    # we failed to create a thread that the user could
                    # read our response in, so we're done here.  Abort!
                    return

                # log the mention, now that we know the channel
                # we want to reply to
                if is_summon and isinstance(message, ChannelMessage):
                    # we need to hack up the channel id, since it
                    # might now be a thread.  We want to watch the
                    # thread, not the original channel for unsolicited
                    # responses.
                    if isinstance(response_channel, discord.Thread):
                        message.channel_id = response_channel.id
                    self.decide_to_respond.log_mention(message)

                image_task = None
                if self.image_generator is not None and image_prompt is not None:
                    image_task = await self.image_generator.generate_image(
                        image_prompt,
                        raw_message,
                        response_channel=response_channel,
                    )

                response_tasks = [
                    task for task in [message_task, image_task] if task is not None
                ]
                await asyncio.wait(response_tasks)

        except Exception as e:
            get_logger().error(
                f"Exception while processing message: {e}", exc_info=True
            )

    async def send_response(
        self,
        message: GenericMessage,
        raw_message: discord.Message,
        image_requested: bool,
    ) -> typing.Tuple[asyncio.Task | None, discord.abc.Messageable | None]:
        """
        Send a response to a message.

        Returns a tuple of the task that was created to send the message,
        and the channel that the message was sent to.

        If no message was sent, the task and channel will be None.
        """
        response_channel = raw_message.channel
        if (
            self.reply_in_thread
            and isinstance(raw_message.channel, discord.TextChannel)
            and isinstance(raw_message.author, discord.Member)
        ):
            # we want to create a response thread, if possible
            # but we have to see if the user has permission to do so
            # if the user can't we wont respond at all.
            perms = raw_message.channel.permissions_for(raw_message.author)
            if perms.create_public_threads:
                response_channel = await raw_message.create_thread(
                    name=f"{self.ai_name}: Response to {raw_message.author.name}",
                )
                get_logger().debug(
                    f"Created response thread {response_channel.name} "
                    f"in {raw_message.channel.name}"
                )
            else:
                # This user can't create threads, so we won't resond.
                # The reason we don't respond in the channel is that
                # it can create confusion later if a second user who
                # DOES have thread-create permission replies to that
                # message.  We'd end up creating a thread for that
                # second user's response, and again for a third user,
                # etc.
                get_logger().debug("User can't create threads, not responding.")
                return (None, None)

        response_coro = self.send_response_in_channel(
            message=message,
            raw_message=raw_message,
            image_requested=image_requested,
            response_channel=response_channel,
        )
        response_task = asyncio.create_task(response_coro)
        return (response_task, response_channel)

    async def history_plus_thread_kickoff_message(
        self,
        aiter: typing.AsyncIterator[discord.Message],
        limit: int,
    ) -> typing.AsyncIterator[GenericMessage]:
        """
        When returning the history of a thread, Discord
        does not include the message that kicked off the thread.

        It will show it in the UI as if it were, but it's not
        one of the messages returned by the history iterator.

        This method attempts to return that message as well,
        if we need it.
        """
        items = 0
        last_returned = None
        async for item in aiter:
            last_returned = item
            yield discord_message_to_generic_message(item)
            items += 1
        if last_returned is not None and items < limit:
            # we've reached the beginning of the history, but
            # still have space.  If this message was a reply
            # to another message, return that message as well.
            if last_returned.reference is not None:
                ref = last_returned.reference.resolved
                if ref is not None and isinstance(ref, discord.Message):
                    yield discord_message_to_generic_message(ref)

    async def recent_messages_following_thread(
        self, channel: discord.abc.Messageable
    ) -> typing.AsyncIterator[GenericMessage]:
        history = channel.history(limit=self.prompt_generator.history_lines)
        result = self.history_plus_thread_kickoff_message(
            history,
            limit=self.prompt_generator.history_lines,
        )
        return result

    async def send_response_in_channel(
        self,
        message: GenericMessage,
        raw_message: discord.Message,
        image_requested: bool,
        response_channel: discord.abc.Messageable,
    ) -> None:
        get_logger().debug(f"Request from {message.author_name}")

        recent_messages = await self.recent_messages_following_thread(response_channel)

        repeated_id = self.repetition_tracker.get_throttle_message_id(
            raw_message.channel.id
        )

        prompt_prefix = await self.prompt_generator.generate(
            ai_user_id=self.ai_user_id,
            message_history=recent_messages,
            image_requested=image_requested,
            throttle_message_id=repeated_id,
        )

        response_stats = self.aggregate_response_stats.log_request_arrived(
            prompt_prefix
        )
        if self.log_all_the_things:
            print("prompt_prefix:\n----------\n")
            print(prompt_prefix)
            print("Response:\n----------\n")

        try:
            if self.dont_split_responses:
                generator = self.ooba_client.request_as_string(prompt_prefix)
            else:
                generator = self.ooba_client.request_by_sentence(prompt_prefix)

            async for sentence in generator:
                if self.log_all_the_things:
                    print(sentence)

                # if the AI gives itself a second line, just ignore
                # the line instruction and continue
                if self.prompt_generator.bot_prompt_line == sentence:
                    get_logger().warning(
                        f'Filtered out "{sentence}" from response, continuing'
                    )
                    continue

                # hack: abort response if it looks like the AI is
                # continuing the conversation as someone else
                if sentence.endswith(" says:"):
                    get_logger().warning(
                        f'Filtered out "{sentence}" from response, aborting'
                    )
                    break

                response_message = await response_channel.send(sentence)
                generic_response_message = discord_message_to_generic_message(
                    response_message
                )
                self.repetition_tracker.log_message(
                    raw_message.channel.id, generic_response_message
                )

                response_stats.log_response_part()

        except Exception as err:
            get_logger().error(f"Error: {str(err)}")
            self.aggregate_response_stats.log_response_failure()
            return

        response_stats.write_to_log(f"Response to {message.author_name} done!  ")
        self.aggregate_response_stats.log_response_success(response_stats)
