import asyncio
import importlib
import os
import random
import sys
import uuid

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# .env loading (works with or without the python-dotenv package)
# ---------------------------------------------------------------------------

def _manual_load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_dotenv_spec = importlib.util.find_spec("dotenv")
if _dotenv_spec is not None:
    load_dotenv = importlib.import_module("dotenv").load_dotenv
else:
    load_dotenv = _manual_load_dotenv

load_dotenv()
if os.path.exists(".env.local"):
    try:
        load_dotenv(".env.local")
    except TypeError:
        _manual_load_dotenv(".env.local")

TOKEN = (
    os.getenv("DISCORD_TOKEN")
    or os.getenv("TOKEN")
    or os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or ""
).strip()

# ---------------------------------------------------------------------------
# Discord intents
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

class ImposterGame:
    MIN_PLAYERS = 3

    with open("words.txt", encoding="utf-8") as _f:
        SECRET_WORDS: list[str] = [line.strip() for line in _f if line.strip()]

    # Class-level word queue shared across all game instances so words don't
    # repeat until every word in the list has been used at least once.
    _word_queue: list[str] = []
    _last_word: str | None = None

    @classmethod
    def _next_word(cls) -> str:
        """Pop from a shuffled queue; refill when empty; never repeat last word."""
        if not cls._word_queue:
            cls._word_queue = cls.SECRET_WORDS.copy()
            random.shuffle(cls._word_queue)

        # If the next word would be the same as the last one, swap it to the back.
        if len(cls._word_queue) > 1 and cls._word_queue[-1] == cls._last_word:
            cls._word_queue.insert(0, cls._word_queue.pop())

        word = cls._word_queue.pop()
        cls._last_word = word
        return word

    def __init__(self) -> None:
        self.players: list[discord.Member] = []
        self.alive_players: list[discord.Member] = []
        self.roles: dict[int, str] = {}
        self.is_active: bool = False
        self.host_id: int | None = None
        self.imposter: discord.Member | None = None
        self.secret_word: str | None = None
        self.first_player: discord.Member | None = None

    def reset(self) -> None:
        """Clear all state so a fresh game can start."""
        self.players.clear()
        self.alive_players.clear()
        self.roles.clear()
        self.is_active = False
        self.host_id = None
        self.imposter = None
        self.secret_word = None
        self.first_player = None

    def begin(self) -> tuple[discord.Member, str]:
        """Assign roles, pick a word, mark the game active, and return (imposter, word)."""
        self.imposter = random.choice(self.players)
        self.secret_word = self._next_word()
        self.roles = {
            p.id: ("Imposter" if p.id == self.imposter.id else "Crewmate")
            for p in self.players
        }
        self.alive_players = list(self.players)
        self.first_player = random.choice(self.players)
        self.is_active = True
        return self.imposter, self.secret_word

    @property
    def player_names(self) -> str:
        return ", ".join(p.display_name for p in self.players) or "Nobody yet"


# Per-guild game registry
_games: dict[int, ImposterGame] = {}


def get_guild_game(guild_id: int) -> ImposterGame:
    if guild_id not in _games:
        _games[guild_id] = ImposterGame()
    return _games[guild_id]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GuildChannel = discord.TextChannel | discord.Thread


async def safe_reply(
    interaction: discord.Interaction,
    message: str,
    *,
    ephemeral: bool = True,
) -> None:
    """Respond to an interaction whether or not it has already been acknowledged."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)
    except Exception as exc:
        print(f"[safe_reply] Could not send message: {exc}")


async def eject_player(
    player: discord.Member,
    channel: discord.TextChannel | discord.Thread,
    game: ImposterGame,
) -> None:
    """Mute the ejected player in the text channel, reveal their role, and handle voice."""
    was_imposter = game.roles.get(player.id) == "Imposter"
    role_reveal = "**L'Imposter** 🔪" if was_imposter else "**Crewmate** ✅"
    verdict = (
        "L'Imposter tqbd 3lih! w ntoma rb7to! 🎉!"
        if was_imposter
        else "A7a.. ghlto! L'Imposter mzal 3endkum... 😈 Dir balek 3la rassek!"
    )

    # --- Text-channel mute ---
    muted = False
    try:
        await channel.set_permissions(
            player,
            send_messages=False,
            reason="Ejected by game vote - muted for 30 s.",
        )
        muted = True
    except discord.Forbidden:
        print(
            f"[EJECT] Missing 'Manage Permissions' to mute {player.display_name}. "
            "Grant the bot that permission."
        )
    except discord.HTTPException as exc:
        print(f"[EJECT] Permission update failed for {player.display_name}: {exc}")

    mute_notice = " *(muted 30s - sktna wa7d chwiya 😶)*" if muted else ""
    try:
        await channel.send(
            f"**{player.display_name}**{mute_notice}\n"
            f"{player.display_name} kan {role_reveal}.\n"
            f"{verdict}"
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"[EJECT] Failed to send punishment message: {exc}")

    # Schedule text-channel unmute
    if muted:
        async def _unmute_text() -> None:
            await asyncio.sleep(30)
            try:
                await channel.set_permissions(
                    player,
                    send_messages=None,
                    reason="30 s elapsed - restoring permissions.",
                )
                await channel.send(
                    f"⏱️ {player.mention} l'mute dyalek sala! Rja3 thddr"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        asyncio.create_task(_unmute_text())

    # --- Voice punishment when a crewmate was wrongly ejected ---
    if not was_imposter and game.imposter is not None:
        voice_channel: discord.VoiceChannel | None = None
        for p in list(game.alive_players) + [player]:
            if p.voice and p.voice.channel:
                voice_channel = p.voice.channel  # type: ignore[assignment]
                break

        if voice_channel is not None:
            imposter = game.imposter
            voice_muted: list[discord.Member] = []

            for vc_member in voice_channel.members:
                if vc_member.id == imposter.id:
                    try:
                        if vc_member.voice and vc_member.voice.mute:
                            await vc_member.edit(mute=False, reason="Imposter wins this round.")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                else:
                    try:
                        await vc_member.edit(mute=True, reason="Wrong ejection - muted 30 s.")
                        voice_muted.append(vc_member)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

            try:
                await channel.send(
                    f"🔇 **Ghltou! L'Imposter mazal 7or!**\n"
                    f"**{imposter.display_name}** bo7do li 3endo l'mic - yall are muted 😈"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

            async def _unmute_voice() -> None:
                await asyncio.sleep(30)
                for vc_member in voice_muted:
                    try:
                        await vc_member.edit(mute=False, reason="30 s elapsed - voice unmuted.")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                try:
                    await channel.send("⏱️ L'30s salat! Rj3ou thdrou!")
                except (discord.Forbidden, discord.HTTPException):
                    pass

            asyncio.create_task(_unmute_voice())


# ---------------------------------------------------------------------------
# Vote UI
# ---------------------------------------------------------------------------

class VoteButton(discord.ui.Button):
    """One button per candidate player."""

    def __init__(self, candidate: discord.Member, session_id: str) -> None:
        super().__init__(
            label=candidate.display_name,
            # Include session_id so stale buttons from timed-out sessions
            # can never affect a new vote session.
            custom_id=f"vote_{session_id}_{candidate.id}",
            style=discord.ButtonStyle.primary,
        )
        self.candidate = candidate

    async def callback(self, interaction: discord.Interaction) -> None:
        view: VoteView = self.view  # type: ignore[assignment]
        voter_id = interaction.user.id

        if voter_id not in view.eligible_voter_ids:
            await interaction.response.send_message(
                "🛑Ghi lli dakhlin f l'game yqdru yvotiu!",
                ephemeral=True,
            )
            return

        if voter_id in view.votes:
            await interaction.response.send_message(
                "Bro rak deja Voteti! ⚠️",
                ephemeral=True,
            )
            return

        view.votes[voter_id] = self.candidate.id
        view.tally[self.candidate.id] = view.tally.get(self.candidate.id, 0) + 1

        remaining = len(view.eligible_voter_ids) - len(view.votes)
        await interaction.response.send_message(
            f"✅ Votiti 3la **{self.candidate.display_name}** - wakha, nchofou!\n"
            f"*(baqyin {remaining} vote(s))*",
            ephemeral=True,
        )

        if len(view.votes) >= len(view.eligible_voter_ids):
            await view.finish(interaction)


class VoteView(discord.ui.View):
    """
    Renders one button per alive player.
    Tracks votes, prevents double-voting, and resolves when
    everyone has voted or the timeout fires.
    """

    def __init__(
        self,
        game: ImposterGame,
        alive_players: list[discord.Member],
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.game = game
        # Snapshot of alive players at vote-start for consistent display.
        self.alive_players: list[discord.Member] = list(alive_players)
        self.eligible_voter_ids: set[int] = {p.id for p in self.alive_players}
        self.votes: dict[int, int] = {}   # voter_id  -> candidate_id
        self.tally: dict[int, int] = {}   # candidate_id -> vote count
        self.message: discord.Message | None = None
        self._finished = False

        session_id = uuid.uuid4().hex[:8]
        for player in self.alive_players:
            self.add_item(VoteButton(player, session_id))

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]

    def get_most_voted(self) -> discord.Member | None:
        """Return the player with the most votes, or None on tie/empty tally."""
        if not self.tally:
            return None
        max_votes = max(self.tally.values())
        top = [uid for uid, cnt in self.tally.items() if cnt == max_votes]
        # Tie → nobody gets ejected (fair rule)
        if len(top) > 1:
            return None
        return next((p for p in self.alive_players if p.id == top[0]), None)

    def build_results_text(self, most_voted: discord.Member | None) -> str:
        """Return a formatted results string (pure - no side effects)."""
        lines = ["📊 **Results - chkoun gha ytchd?**"]
        for player in self.alive_players:
            count = self.tally.get(player.id, 0)
            marker = " 👈 ): **TCHDpush --force*" if player == most_voted else ""
            lines.append(f"• {player.display_name}: {count} vote(s){marker}")

        if most_voted is None and self.tally:
            lines.append("\n🤝 **Tie! ta 7d ma tchd!**")
        elif most_voted:
            was_imposter = self.game.roles.get(most_voted.id) == "Imposter"
            verdict = (
                "🎉 L'Imposter tqbd 3lih! w ntoma rb7to!"
                if was_imposter
                else "😈 A7a chdito Innocent! L'Imposter mazal 7or binkum - dir balek!"
            )
            lines.append(f"\n{verdict}")

        return "\n".join(lines)

    async def _resolve(
        self,
        most_voted: discord.Member | None,
        message: discord.Message | None,
        channel: discord.TextChannel | discord.Thread | None,
    ) -> None:
        """Shared resolution logic for finish() and on_timeout()."""
        # Remove ejected player from the live game state.
        if most_voted and most_voted in self.game.alive_players:
            self.game.alive_players.remove(most_voted)

        results = self.build_results_text(most_voted)

        if message is not None:
            try:
                await message.edit(content=results, view=self)
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"[VOTE] Could not edit vote message: {exc}")

        if most_voted and isinstance(channel, (discord.TextChannel, discord.Thread)):
            await eject_player(most_voted, channel, self.game)
        elif most_voted:
            print(f"[VOTE] Unexpected channel type {type(channel)} - eject skipped.")

    async def finish(self, interaction: discord.Interaction) -> None:
        """Called when all eligible players have voted."""
        if self._finished:
            return
        self._finished = True
        self._disable_all()
        self.stop()

        await self._resolve(
            self.get_most_voted(),
            interaction.message,
            interaction.channel,  # type: ignore[arg-type]
        )

    async def on_timeout(self) -> None:
        """Called by discord.py when the 60 s window expires."""
        if self._finished:
            return
        self._finished = True
        self._disable_all()
        self.stop()

        most_voted = self.get_most_voted()
        # Append timeout notice to the message
        original_text = self.build_results_text(most_voted)
        timeout_text = original_text + "\n\n⏰ *L'wa9t sala - 60s w ma votitouch kamlin!*"

        # Patch message manually since we have no interaction here.
        if self.message is not None:
            try:
                await self.message.edit(content=timeout_text, view=self)
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"[VOTE] Could not edit timed-out vote message: {exc}")

        # Still eject if there was a clear winner.
        if most_voted and most_voted in self.game.alive_players:
            self.game.alive_players.remove(most_voted)

        channel = self.message.channel if self.message else None
        if most_voted and isinstance(channel, (discord.TextChannel, discord.Thread)):
            await eject_player(most_voted, channel, self.game)

        print("[VOTE] Vote timed out.")


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class MafiaBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        self.tree.on_error = self.on_app_command_error

        sync = os.getenv("SYNC_COMMANDS", "0").strip() == "1"
        if not sync:
            print("Command sync skipped. Set SYNC_COMMANDS=1 to force a sync.")
            return

        guild_id_str = os.getenv("GUILD_ID", "").strip()
        try:
            if guild_id_str:
                guild = discord.Object(id=int(guild_id_str))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"Synced {len(synced)} command(s) to guild {guild_id_str}.")
            else:
                synced = await self.tree.sync()
                print(f"Synced {len(synced)} global command(s).")
        except discord.Forbidden:
            print(
                "[ERROR] 403 Missing Access - bot needs the 'applications.commands' scope.\n"
                "Re-invite with: https://discord.com/oauth2/authorize"
                "?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=8"
            )
        except discord.HTTPException as exc:
            print(f"[ERROR] Command sync failed: {exc}")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to new guild {guild.id}.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[ERROR] Could not sync to guild {guild.id}: {exc}")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Latency: {round(self.latency * 1000)} ms")
        print("------")

    async def on_message(self, message: discord.Message) -> None:
        # Hard-ignore prefix help so the bot never responds to !help.
        if message.author.bot:
            return

        content = message.content.strip().lower()
        if content == "!help" or content.startswith("!help "):
            return

        await self.process_commands(message)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        cmd_name = interaction.command.name if interaction.command else "?"
        print(f"[APP CMD ERROR] /{cmd_name}: {error}")
        if isinstance(error, app_commands.CheckFailure):
            msg = "Had command kaykhdem ghir f server channels (machi f DM)."
        else:
            msg = f"Kayn moshkil: `{error}`"
        await safe_reply(interaction, msg, ephemeral=True)


bot = MafiaBot()


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    quality = "Nadii 🔥" if latency_ms < 100 else "chwiya 3la 9d l7al 😅"
    await interaction.response.send_message(
        f"Ana hna! Latency: **{latency_ms} ms** - {quality}",
        ephemeral=True,
    )


@bot.tree.command(name="join", description="Join the current game lobby.")
@app_commands.guild_only()
async def join(interaction: discord.Interaction) -> None:
    game = get_guild_game(interaction.guild_id)
    member = interaction.guild.get_member(interaction.user.id) or interaction.user

    if game.is_active:
        await interaction.response.send_message(
            "⚠️ L'game deja bdat! Tsna had round ysali w aji t'join!", ephemeral=True
        )
        return

    if any(p.id == member.id for p in game.players):
        await interaction.response.send_message("😂 Nta rak deja hna!", ephemeral=True)
        return

    if not game.players:
        game.host_id = member.id

    game.players.append(member)

    host_tag = " 👑 **L'Boss**" if member.id == game.host_id else ""
    await interaction.response.send_message(
        f"{member.mention}{host_tag} d5al l'lobby!\n"
        f"**Players ({len(game.players)}):** {game.player_names}"
    )


@bot.tree.command(name="leave", description="Leave the lobby before the game starts.")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction) -> None:
    game = get_guild_game(interaction.guild_id)
    member = interaction.guild.get_member(interaction.user.id) or interaction.user

    if game.is_active:
        await interaction.response.send_message(
            "⚠️ L'game deja bdat - ma yimkanch tkhroj daba!", ephemeral=True
        )
        return

    player = next((p for p in game.players if p.id == member.id), None)
    if player is None:
        await interaction.response.send_message("🤔 Nta machi f l'lobby!", ephemeral=True)
        return

    game.players.remove(player)

    # Transfer host to the next player if the host left
    if game.host_id == member.id:
        if game.players:
            game.host_id = game.players[0].id
            new_host = game.players[0]
            await interaction.response.send_message(
                f"👋 {member.display_name} khroj mn l'lobby.\n"
                f"👑 {new_host.mention} daba nta l'boss!"
            )
        else:
            game.host_id = None
            await interaction.response.send_message(
                f"👋 {member.display_name} khrj - l'lobby khawi daba."
            )
    else:
        await interaction.response.send_message(
            f"👋 {member.display_name} khrj mn l'lobby.\n"
            f"**Players ({len(game.players)}):** {game.player_names}"
        )


@bot.tree.command(name="players", description="Show who is currently in the lobby or game.")
@app_commands.guild_only()
async def players_cmd(interaction: discord.Interaction) -> None:
    game = get_guild_game(interaction.guild_id)

    if not game.players:
        await interaction.response.send_message(
            "😶 L'lobby khawi - Dir`/join` bach tbda.",
            ephemeral=True,
        )
        return

    host = interaction.guild.get_member(game.host_id) if game.host_id else None
    host_name = host.display_name if host else "?"
    status = "🟢 Game active" if game.is_active else "🟡 Waiting to start"

    lines = [f"**{status} - Boss: {host_name}**", ""]
    for p in game.players:
        crown = " 👑" if p.id == game.host_id else ""
        alive_tag = ""
        if game.is_active:
            alive_tag = " ✅" if p in game.alive_players else " 💀"
        lines.append(f"• {p.display_name}{crown}{alive_tag}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="start", description="Start the game (host only).")
@app_commands.guild_only()
async def start(interaction: discord.Interaction) -> None:
    try:
        game = get_guild_game(interaction.guild_id)
        member = interaction.guild.get_member(interaction.user.id) or interaction.user

        if game.host_id is None:
            await interaction.response.send_message(
                "La Walo, ma7ad mzal ma ja! Gol lnas ijo y'join b `/join`!",
                ephemeral=True,
            )
            return

        if member.id != game.host_id:
            host = interaction.guild.get_member(game.host_id)
            host_name = host.display_name if host else f"<ID {game.host_id}>"
            await interaction.response.send_message(
                f"🛑 Ghi **{host_name}** l'boss yqder y'bda l'game!", ephemeral=True
            )
            return

        if game.is_active:
            await interaction.response.send_message(
                "⚠️ L'game deja badya!", ephemeral=True
            )
            return

        if len(game.players) < ImposterGame.MIN_PLAYERS:
            await interaction.response.send_message(
                f"Khask **{ImposterGame.MIN_PLAYERS} nas** 3la laqal. "
                f"Daba 3endna {len(game.players)} - zid chi nas bach t7la l'game!",
                ephemeral=True,
            )
            return

        # Defer early so Discord doesn't time out while we send DMs.
        await interaction.response.defer()

        # begin() sets is_active=True and picks roles atomically - no race window.
        imposter, secret_word = game.begin()

        await interaction.followup.send(
            f"🔥 **L'game bdat! {len(game.players)} nas dakhlin!**\n"
            f"Kol wa7ed ghadi ywslo DM - qrah mzyan. Thla! 😈"
        )

        dm_failed: list[str] = []
        for player in game.players:
            if player.id == imposter.id:
                content = (
                    "🔪 **Nta huwa L'Imposter!**\n"
                    "Maghadich tkun 3arf secret word.\n"
                    "Dir rassek bhal ila 3arf, w khllihum may3i9ouch bik 😏"
                )
            else:
                content = (
                    f"✅ **Nta Crewmate a s7abi!**\n"
                    f"Secret word dyalek hiya: **{secret_word}** 🤫\n"
                    f"Sta3mleha bach t3ref s7abek - walakin dir balek, "
                    f"L'Imposter kayen binatkum w kaytsenna l'ghalta dyalek!"
                )
            try:
                await player.send(content)
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"[START] Could not DM {player.display_name}: {exc}")
                dm_failed.append(player.mention)

        if dm_failed:
            await interaction.followup.send(
                f"⚠️ Had nas ma qderch nweslhom DM (DMs msdoda): {', '.join(dm_failed)}\n"
                f"Khas y7ello DMs dyalhom bach il3bu - hshuma 3likhom! 😤",
                ephemeral=False,
            )

        first = game.first_player
        first_hint = (
            "*(dir balek, L'Imposter kayen! 👀)*"
            if first.id != imposter.id
            else "*(Wa dir chi guess mzyan 😈)*"
        )
        await interaction.followup.send(
            f"🎲 **L'qur3a dart!**\n"
            f"{first.mention} nta lawal! Gol wa7ed 7aja 3la l'kelma 🎤 {first_hint}"
        )

    except Exception as exc:
        print(f"[ERROR] /start failed: {exc}")
        await safe_reply(
            interaction,
            "Kayn moshkil f /start daba. 3awed jerrb chwiya mn b3d.",
            ephemeral=True,
        )


@bot.tree.command(name="vote", description="Open a vote to eliminate a player (host only).")
@app_commands.guild_only()
async def vote(interaction: discord.Interaction) -> None:
    game = get_guild_game(interaction.guild_id)
    member = interaction.guild.get_member(interaction.user.id) or interaction.user

    if not game.is_active:
        await interaction.response.send_message(
            "😴 Ma kayna 7ta game badya daba! Bda b `/start`!", ephemeral=True
        )
        return

    if game.host_id is None or member.id != game.host_id:
        host = interaction.guild.get_member(game.host_id) if game.host_id else None
        host_name = host.display_name if host else "unknown"
        await interaction.response.send_message(
            f"🛑 Ghir **{host_name}** l'boss yqder yftah vote!", ephemeral=True
        )
        return

    if len(game.alive_players) < 2:
        await interaction.response.send_message(
            "😅 khas ktr mn 3 d nas bach tkmlo tr7!", ephemeral=True
        )
        return

    view = VoteView(game=game, alive_players=list(game.alive_players), timeout=60.0)
    player_list = ", ".join(p.display_name for p in game.alive_players)

    await interaction.response.send_message(
        f"🗳️ **Vote daba - chkoun L'Imposter?!**\n"
        f"Lli kayin: {player_list}\n"
        f"3endkum **60 s** - vote dghya! ⏰",
        view=view,
    )

    # Store the message reference so on_timeout can edit it without an interaction.
    view.message = await interaction.original_response()


@bot.tree.command(name="reset", description="Reset the lobby so a new game can start (host only).")
@app_commands.guild_only()
async def reset(interaction: discord.Interaction) -> None:
    game = get_guild_game(interaction.guild_id)
    member = interaction.guild.get_member(interaction.user.id) or interaction.user

    if game.host_id is not None and member.id != game.host_id:
        host = interaction.guild.get_member(game.host_id)
        host_name = host.display_name if host else f"<ID {game.host_id}>"
        await interaction.response.send_message(
            f"🛑 Ghi **{host_name}** l'boss yqder ydir reset!", ephemeral=True
        )
        return

    game.reset()
    await interaction.response.send_message(
        "🔄 Lobby dart reset! Ida bghiti t3awd dir `/join` 💪"
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if not TOKEN:
    print(
        "[CONFIG ERROR] Missing bot token.\n"
        "Set DISCORD_TOKEN (or TOKEN / DISCORD_BOT_TOKEN / BOT_TOKEN) in your .env file."
    )
    sys.exit(1)

bot.run(TOKEN)
