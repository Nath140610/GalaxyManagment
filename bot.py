import asyncio
import json
import os
import random
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from openai import APIError, OpenAI, OpenAIError


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("La variable DISCORD_TOKEN est obligatoire dans .env.")

CONFIG_PATH = Path(__file__).with_name("guild_config.json")
DEFAULT_CONFIG = {
    "staff_role_id": None,
    "log_channel_id": None,
    "send_channel_id": None,
    "ticket_category_id": None,
    "ticket_founder_role_id": None,
    "ticket_cofounder_role_id": None,
}
STAFF_ROLE_LOCK_CODE = "HT156494SERV269+8'è-é\"àNTR"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
OPENAI_STATE_PATH = Path(__file__).with_name("openai_state.json")
STATS_PATH = Path(__file__).with_name("stats.json")

LOCAL_AI_PATTERNS = [
    (
        ("bonjour", "salut"),
        [
            "Salut ! Dis-moi ce dont tu as besoin et je fais de mon mieux.",
            "Hello ici, tu veux discuter d'un ticket ?",
            "Coucou ! Je suis là pour prendre ta question.",
        ],
    ),
    (
        ("probleme", "bug", "erreur"),
        [
            "Je suis désolé(e) pour ce souci, peux-tu préciser le contexte ?",
            "Un bug a été détecté, merci de détailler ce qui se passe.",
        ],
    ),
    (
        ("demande", "question"),
        [
            "Merci pour ta question, je la transmets au staff.",
            "Je note ta demande et je t'appelle dès qu'un staff arrive.",
        ],
    ),
    (
        ("merci",),
        [
            "Avec plaisir ! Si tu as une autre question, n'hésite pas.",
            "Merci à toi aussi, je reste dispo pour le staff.",
        ],
    ),
]
ticket_ai_responded: set[int] = set()
pending_ticket_data: dict[int, dict] = {}


def read_openai_state() -> dict:
    if not OPENAI_STATE_PATH.exists():
        return {"disabled": False, "reason": None}

    try:
        return json.loads(OPENAI_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"disabled": False, "reason": None}


def write_openai_state(state: dict) -> None:
    OPENAI_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def is_openai_disabled() -> bool:
    return read_openai_state().get("disabled", False)


def mark_openai_disabled(reason: str) -> None:
    write_openai_state(
        {
            "disabled": True,
            "reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def mark_openai_enabled() -> None:
    write_openai_state({"disabled": False, "reason": None})


def load_stats() -> dict:
    if not STATS_PATH.exists():
        return {}
    try:
        return json.loads(STATS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_stats(stats: dict) -> None:
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")


def init_guild_stats(stats: dict, guild_id: int) -> dict:
    guild_key = str(guild_id)
    guild_stats = stats.setdefault(
        guild_key,
        {
            "events": [],
            "ticket_history": [],
            "author_counts": {},
            "channel_activity": {},
        },
    )
    guild_stats.setdefault("events", [])
    guild_stats.setdefault("ticket_history", [])
    guild_stats.setdefault("author_counts", {})
    guild_stats.setdefault("channel_activity", {})
    return guild_stats


def record_stats_event(
    guild_id: int,
    event_type: str,
    *,
    author_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    stats = load_stats()
    guild_stats = init_guild_stats(stats, guild_id)

    guild_stats["events"].append(
        {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "channel_id": channel_id,
            **(metadata or {}),
        }
    )

    if author_id:
        authors = guild_stats["author_counts"]
        auth = authors.setdefault(str(author_id), {"ban": 0, "mute": 0, "ticket": 0})
        auth[event_type] = auth.get(event_type, 0) + 1

    if channel_id:
        activity = guild_stats["channel_activity"]
        activity[str(channel_id)] = activity.get(str(channel_id), 0) + 1

    save_stats(stats)


def bucket_stats(
    events: list[dict], days_count: int = 7, start_offset_days: int = 0
) -> tuple[list[str], dict[str, list[int]]]:
    now = datetime.now(timezone.utc) - timedelta(days=start_offset_days)
    days = [
        (now - timedelta(days=delta)).date() for delta in reversed(range(days_count))
    ]
    labels = [day.strftime("%d %b") for day in days]
    counts = {
        "ban": [0] * days_count,
        "mute": [0] * days_count,
        "ticket": [0] * days_count,
        "join": [0] * days_count,
    }

    for event in events:
        ts = event.get("timestamp")
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        day = dt.date()
        if day in days:
            index = days.index(day)
            if event.get("type") in counts:
                counts[event["type"]][index] += 1

    return labels, counts


async def build_stats_chart(guild_id: int) -> str:
    stats = load_stats()
    guild_stats = stats.get(str(guild_id), {})
    events = guild_stats.get("events", [])
    labels, counts = bucket_stats(events, days_count=7)

    import matplotlib.pyplot as plt

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(8, 4), facecolor="#050509")
    ax.set_facecolor("#050509")
    x = list(range(len(labels)))
    ax.plot(x, counts["ban"], label="Bans", color="#9b59b6", marker="o")
    ax.plot(x, counts["mute"], label="Mutes", color="#8e44ad", marker="o")
    ax.plot(x, counts["ticket"], label="Tickets", color="#5d2682", marker="o", linestyle="--")
    ax.fill_between(x, counts["ticket"], color="#5d2682", alpha=0.2)
    ax.plot(x, counts["join"], label="Membres", color="#f39c12", marker="o", linestyle=":")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_title("Évolution du serveur", color="#ffffff")
    ax.set_xlabel("Jours", color="#dddddd")
    ax.set_ylabel("Occurences", color="#dddddd")
    ax.grid(color="#1f1a2b", linestyle="--", linewidth=0.5)
    ax.legend(facecolor="#1b1124")

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".png", prefix="server-stats-", delete=False
    )
    fig.savefig(temp_file.name, facecolor=fig.get_facecolor())
    plt.close(fig)
    return temp_file.name


def summarize_stats(guild_id: int) -> dict[str, int]:
    stats = load_stats()
    guild_stats = stats.get(str(guild_id), {})
    events = guild_stats.get("events", [])
    _, counts = bucket_stats(events)
    return {key: sum(values) for key, values in counts.items()}


def summarize_details(guild_id: int) -> dict[str, str]:
    totals = summarize_stats(guild_id)
    days = 7
    bans = totals.get("ban", 0)
    mutes = totals.get("mute", 0)
    tickets = totals.get("ticket", 0)
    joins = totals.get("join", 0)
    total_actions = bans + mutes + tickets
    avg_actions = total_actions / days if days else 0
    ratio_ban = f"{(bans / total_actions * 100):.1f}%" if total_actions else "0%"
    ratio_mute = f"{(mutes / total_actions * 100):.1f}%" if total_actions else "0%"
    return {
        "moyenne": f"{avg_actions:.1f} actions/j",
        "distrib_ban": ratio_ban,
        "distrib_mute": ratio_mute,
        "nouveaux_membres": str(joins),
    }


def append_ticket_history_entry(guild_id: int, entry: dict) -> None:
    stats = load_stats()
    guild_stats = init_guild_stats(stats, guild_id)
    guild_stats.setdefault("ticket_history", [])
    guild_stats["ticket_history"].append(entry)
    save_stats(stats)


def recent_ticket_entries(guild_id: int, limit: int = 3) -> list[dict]:
    stats = load_stats()
    guild_stats = stats.get(str(guild_id), {})
    history = guild_stats.get("ticket_history", [])
    return history[-limit:] if history else []


def top_authors(guild_id: int, limit: int = 3) -> list[tuple[int, dict]]:
    stats = load_stats()
    guild_stats = stats.get(str(guild_id), {})
    author_counts = guild_stats.get("author_counts", {})
    return sorted(
        [(int(uid), counts) for uid, counts in author_counts.items()],
        key=lambda item: sum(item[1].values()),
        reverse=True,
    )[:limit]


def week_comparison_summary(guild_id: int) -> str:
    stats = load_stats()
    guild_stats = stats.get(str(guild_id), {})
    events = guild_stats.get("events", [])
    _, current = bucket_stats(events, days_count=7, start_offset_days=0)
    _, previous = bucket_stats(events, days_count=7, start_offset_days=7)
    lines = []
    for key in ("ban", "mute", "ticket", "join"):
        curr_total = sum(current.get(key, []))
        prev_total = sum(previous.get(key, []))
        delta = curr_total - prev_total
        sign = "+" if delta >= 0 else ""
        lines.append(f"{key.capitalize()}: {curr_total} ({sign}{delta} vs sem. passée)")
    return "\n".join(lines)


def format_channel_heatmap(guild_id: int, guild: discord.Guild, limit: int = 3) -> str:
    stats = load_stats()
    guild_stats = stats.get(str(guild_id), {})
    channel_activity = guild_stats.get("channel_activity", {})
    pairs = sorted(
        [
            (int(channel_id), count)
            for channel_id, count in channel_activity.items()
            if count > 0
        ],
        key=lambda item: item[1],
        reverse=True,
    )
    lines = []
    for channel_id, count in pairs[:limit]:
        channel = guild.get_channel(channel_id)
        label = channel.mention if isinstance(channel, discord.TextChannel) else f"#{channel_id}"
        lines.append(f"{label}: {count} tickets")
    return lines and "\n".join(lines) or "Aucun ticket enregistré."


async def send_recap_to_guild(
    guild: discord.Guild,
    *,
    triggered_by: Optional[discord.Member] = None,
    mention_staff: bool = True,
) -> None:
    config = get_guild_config(guild.id)
    log_channel_id = config.get("log_channel_id")
    if not log_channel_id:
        return
    channel = guild.get_channel(log_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    summary = summarize_stats(guild.id)
    detail = summarize_details(guild.id)
    week_comparison = week_comparison_summary(guild.id)
    heatmap = format_channel_heatmap(guild.id, guild)
    recent_entries = recent_ticket_entries(guild.id)
    top3 = top_authors(guild.id)

    recent_lines = []
    for entry in recent_entries:
        channel = guild.get_channel(entry.get("channel_id"))
        mention = channel.mention if isinstance(channel, discord.TextChannel) else "#?"
        content = (entry.get("content") or "(vide)").replace("\n", " ")
        recent_lines.append(f"{mention} : {content[:45]}")
    recent_text = "\n".join(recent_lines) or "Aucun ticket récent"

    top_lines = []
    for uid, counts in top3:
        total = sum(counts.values())
        top_lines.append(f"<@{uid}> ({total} actions)")
    top_text = "\n".join(top_lines) or "Pas encore d'actions"

    description_lines = []
    if mention_staff and config.get("staff_role_id"):
        staff_role = guild.get_role(config["staff_role_id"])
        if staff_role:
            description_lines.append(f"{staff_role.mention} • Weekly recap")
    if triggered_by:
        description_lines.append(f"Demandé par {triggered_by.mention}")

    description = "\n".join(description_lines) or "Voici votre recap hebdomadaire."
    chart_path = await build_stats_chart(guild.id)
    try:
        file = discord.File(chart_path, filename="weekly-stats.png")
        embed = format_embed(
            "Herbo Recap",
            description,
            color=SEND_EMBED_COLOR,
        fields=[
            ("Bans (7j)", str(summary.get("ban", 0)), True),
            ("Mutes (7j)", str(summary.get("mute", 0)), True),
            ("Tickets (7j)", str(summary.get("ticket", 0)), True),
            ("Membres (7j)", str(summary.get("join", 0)), True),
            ("Moyenne journalière", detail["moyenne"], True),
            ("Répartition ban/mute", f"{detail['distrib_ban']} / {detail['distrib_mute']}", True),
            ("Comparatif vs semaine passée", week_comparison, False),
            ("Canaux chauds", heatmap, False),
            ("Derniers tickets", recent_text, False),
            ("Top contributeurs", top_text, False),
        ],
            footer_text="Graphique violet/noir généré automatiquement",
        )
        await channel.send(embed=embed, file=file)
    finally:
        try:
            Path(chart_path).unlink()
        except Exception:
            pass


async def weekly_recap_loop() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        days_ahead = (7 - now.weekday()) % 7
        target = (
            now
            + timedelta(days=days_ahead)
            - timedelta(
                hours=now.hour, minutes=now.minute, seconds=now.second, microseconds=now.microsecond
            )
        )
        if target <= now:
            target += timedelta(days=7)
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        for guild in bot.guilds:
            await send_recap_to_guild(guild)


def load_all_configs() -> dict:
    if not CONFIG_PATH.exists():
        return {}

    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


def save_all_configs(configs: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as config_file:
        json.dump(configs, config_file, indent=2)


def get_guild_config(guild_id: int) -> dict:
    configs = load_all_configs()
    stored_config = configs.get(str(guild_id), {})
    return {**DEFAULT_CONFIG, **stored_config}


def update_guild_config(guild_id: int, **updates: Optional[int]) -> dict:
    configs = load_all_configs()
    guild_key = str(guild_id)
    current_config = {**DEFAULT_CONFIG, **configs.get(guild_key, {})}
    current_config.update(updates)
    configs[guild_key] = current_config
    save_all_configs(configs)
    return current_config


def parse_duration(raw_value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([mhd])", raw_value.strip().lower())
    if not match:
        raise ValueError("Format invalide")

    amount = int(match.group(1))
    unit = match.group(2)

    if unit == "m":
        duration = timedelta(minutes=amount)
    elif unit == "h":
        duration = timedelta(hours=amount)
    else:
        duration = timedelta(days=amount)

    if duration <= timedelta():
        raise ValueError("Duree invalide")

    if duration > timedelta(days=28):
        raise ValueError("La duree maximale est 28 jours")

    return duration


def has_staff_access(member: discord.Member, guild_config: dict) -> bool:
    if member.guild_permissions.administrator:
        return True

    staff_role_id = guild_config.get("staff_role_id")
    if not staff_role_id:
        return False

    return any(role.id == staff_role_id for role in member.roles)


def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    guild_config = get_guild_config(guild.id)
    channel_id = guild_config.get("log_channel_id")
    if not channel_id:
        return None

    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


async def send_log(guild: discord.Guild, message: str) -> None:
    channel = get_log_channel(guild)
    if not channel:
        return

    try:
        await channel.send(message)
    except discord.HTTPException:
        pass


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure(
                "Cette commande est disponible uniquement sur un serveur."
            )

        guild_config = get_guild_config(interaction.guild.id)
        if has_staff_access(interaction.user, guild_config):
            return True

        raise app_commands.CheckFailure(
            "Tu dois etre administrateur ou avoir le role staff configure."
        )

    return app_commands.check(predicate)


def has_permission(member: discord.Member, perm_attr: str) -> bool:
    permissions = getattr(member.guild_permissions, perm_attr, None)
    return bool(permissions)


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure(
                "Cette commande est disponible uniquement sur un serveur."
            )

        if interaction.user.guild_permissions.administrator:
            return True

        raise app_commands.CheckFailure(
            "Tu dois etre administrateur pour utiliser /config."
        )

    return app_commands.check(predicate)


def format_embed(
    title: str,
    description: Optional[str] = None,
    *,
    color: int = 0x1ABC9C,
    fields: Optional[list[tuple[str, str, bool]]] = None,
    footer_text: Optional[str] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description or discord.Embed.Empty,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)

    if footer_text:
        embed.set_footer(text=footer_text)

    return embed


async def respond_embed(
    interaction: discord.Interaction,
    title: str,
    description: Optional[str] = None,
    *,
    color: int = 0x1ABC9C,
    fields: Optional[list[tuple[str, str, bool]]] = None,
    footer_text: Optional[str] = None,
    ephemeral: bool = True,
) -> None:
    embed = format_embed(
        title,
        description,
        color=color,
        fields=fields,
        footer_text=footer_text,
    )

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


async def generate_ticket_ai_response(message: discord.Message) -> str:
    if not OPENAI_CLIENT or is_openai_disabled():
        state = read_openai_state()
        reason = state.get("reason")
        return (
            "L'IA n'est pas disponible pour le moment."
            + (f" ({reason})" if reason else " Ajoute `OPENAI_API_KEY` pour l'activer.")
        )

    try:
        response = await OPENAI_CLIENT.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Tu es un assistant de support patient. Tu réponds de manière concise aux "
                        "questions posées dans un ticket Discord."
                    ),
                },
                {
                    "role": "user",
                    "content": f"{message.author.display_name} a demandé : {message.content}",
                },
            ],
            temperature=0.5,
        )
        mark_openai_enabled()
        return response.choices[0].message.content.strip()
    except APIError as exc:
        status = getattr(exc, "status_code", None)
        if status == 402:
            mark_openai_disabled("Crédit OpenAI épuisé")
            return "Le crédit OpenAI est épuisé : la réponse automatique est désactivée."
        if status == 429:
            mark_openai_disabled("Quota insuffisant")
            return "Tu as dépassé ton quota OpenAI ; l’IA est désactivée."
        print(f"Erreur IA (API): {exc}")
        return "Je n'arrive pas à contacter l'IA pour l'instant."
    except OpenAIError as exc:
        print(f"Erreur IA: {exc}")
        return "Je n'arrive pas à contacter l'IA pour l'instant."
    except Exception as exc:
        print(f"Erreur IA inattendue: {exc}")
        return "Je n'arrive pas à contacter l'IA pour l'instant."


def generate_local_ai_response(message: discord.Message) -> str:
    text = message.content.lower()
    for keywords, response in LOCAL_AI_PATTERNS:
        if any(keyword in text for keyword in keywords):
            return random.choice(response) if isinstance(response, list) else response

    fallback = [
        "Je ne suis pas sûr de comprendre, peux-tu reformuler ?",
        "Je vois, merci pour ton message. Notre équipe te répondra bientôt.",
        "Merci pour les détails, un membre du staff arrive.",
    ]
    return random.choice(fallback)


class TicketRoleButton(discord.ui.Button):
    def __init__(self, label: str, role_id: int, style: discord.ButtonStyle):
        super().__init__(label=label, style=style)
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await respond_embed(
                interaction,
                "Erreur",
                "Ce bouton ne fonctionne que sur le serveur.",
                color=ERROR_COLOR,
            )
            return

        role = interaction.guild.get_role(self.role_id)
        if not role:
            await respond_embed(
                interaction,
                "Rôle introuvable",
                "Le rôle associé au bouton a été supprimé.",
                color=ERROR_COLOR,
            )
            return

        await respond_embed(
            interaction,
            "Demande envoyée",
            f"{role.mention} a été notifié, {interaction.user.mention}.",
            color=INFO_COLOR,
        )
        target_channel = interaction.channel
        if isinstance(target_channel, discord.TextChannel):
            await target_channel.send(
                f"{role.mention}, {interaction.user.mention} souhaite échanger avec vous ici."
            )


class TicketButtonView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        guild_config = get_guild_config(guild_id)
        staff_role_id = guild_config.get("staff_role_id")
        founder_role_id = guild_config.get("ticket_founder_role_id")
        cofounder_role_id = guild_config.get("ticket_cofounder_role_id")

        if staff_role_id:
            self.add_item(
                TicketRoleButton("Staff", staff_role_id, discord.ButtonStyle.secondary)
            )
        if founder_role_id:
            self.add_item(
                TicketRoleButton("Fondateur", founder_role_id, discord.ButtonStyle.primary)
            )
        if cofounder_role_id:
            self.add_item(
                TicketRoleButton("Co-fonda", cofounder_role_id, discord.ButtonStyle.success)
            )


SUCCESS_COLOR = 0x1ABC9C
ERROR_COLOR = 0xE74C3C
INFO_COLOR = 0x3498DB
SEND_EMBED_COLOR = 0x5865F2

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

config_group = app_commands.Group(
    name="config",
    description="Configure le bot sur ce serveur",
    guild_only=True,
)

ticket_group = app_commands.Group(
    name="configtickets",
    description="Configure la catégorie et les rôles de tickets",
    guild_only=True,
)


@bot.event
async def on_ready() -> None:
    print(f"Connecte en tant que {bot.user}")
    if not hasattr(bot, "_recap_task"):
        bot._recap_task = bot.loop.create_task(weekly_recap_loop())


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.guild:
        record_stats_event(member.guild.id, "join")


@bot.event
async def setup_hook() -> None:
    await bot.tree.sync()

async def send_ticket_welcome(channel: discord.TextChannel) -> None:
    embed = discord.Embed(
        title="Bienvenue dans votre ticket",
        description="Bienvenue dans votre ticket ! Avec qui voulez-vous parler ?",
        color=INFO_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Choisissez un rôle ci-dessous pour vous mettre en relation.")

    view = TicketButtonView(channel.guild.id)

    await channel.send(embed=embed, view=view)


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
    if not isinstance(channel, discord.TextChannel):
        return

    guild_config = get_guild_config(channel.guild.id)
    ticket_category_id = guild_config.get("ticket_category_id")
    if not ticket_category_id:
        return

    if channel.category_id != ticket_category_id:
        return

    await send_ticket_welcome(channel)


@bot.event
async def on_message(message: discord.Message) -> None:
    await bot.process_commands(message)
    if message.author.bot or not message.guild:
        return

    guild_config = get_guild_config(message.guild.id)
    ticket_category_id = guild_config.get("ticket_category_id")
    if not ticket_category_id:
        return

    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return

    if channel.category_id != ticket_category_id:
        return

    if channel.id in ticket_ai_responded:
        return

    pending_ticket_data[channel.id] = {
        "author_id": message.author.id,
        "channel_id": channel.id,
        "created_at": message.created_at.isoformat(),
        "content": message.content,
    }

    async for previous in channel.history(limit=20, before=message.created_at):
        if not previous.author.bot:
            return

    ai_answer = (
        await generate_ticket_ai_response(message)
        if OPENAI_CLIENT and not is_openai_disabled()
        else generate_local_ai_response(message)
    )
    embed = format_embed(
        "Réponse automatique",
        ai_answer,
        color=INFO_COLOR,
        footer_text="Message généré par l'IA",
    )
    await channel.send(embed=embed)
    ticket_ai_responded.add(channel.id)
    pending = pending_ticket_data.pop(channel.id, None)
    response_timestamp = datetime.now(timezone.utc).isoformat()
    response_time = 0.0
    if pending:
        try:
            created = datetime.fromisoformat(pending["created_at"])
            response_time = (datetime.now(timezone.utc) - created).total_seconds()
        except Exception:
            response_time = 0.0
        entry = {
            "channel_id": channel.id,
            "author_id": pending.get("author_id"),
            "created_at": pending.get("created_at"),
            "responded_at": response_timestamp,
            "response_time": response_time,
            "response_mode": "OpenAI" if OPENAI_CLIENT and not is_openai_disabled() else "local",
            "content": pending.get("content"),
            "response": ai_answer,
        }
        append_ticket_history_entry(channel.guild.id, entry)
    record_stats_event(
        channel.guild.id,
        "ticket",
        author_id=message.author.id,
        channel_id=channel.id,
        metadata={"response_time": response_time},
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CheckFailure):
        title = "Accès refusé"
        description = str(error)
    else:
        print(f"Erreur commande: {error}")
        title = "Erreur interne"
        description = "Une erreur est survenue pendant l'execution de la commande."

    await respond_embed(
        interaction,
        title,
        description,
        color=ERROR_COLOR,
        footer_text="Si le problème persiste, vérifie les permissions.",
    )


@bot.tree.command(name="ban", description="Bannit un membre du serveur")
@staff_only()
@app_commands.describe(utilisateur="Membre a bannir", raison="Raison du ban")
async def ban_command(
    interaction: discord.Interaction,
    utilisateur: discord.Member,
    raison: Optional[str] = None,
) -> None:
    assert interaction.guild is not None
    assert isinstance(interaction.user, discord.Member)

    raison = raison or "Aucune raison fournie"

    if utilisateur.id == interaction.user.id:
        await respond_embed(
            interaction,
            "Interdit",
            "Tu ne peux pas te bannir toi-meme.",
            color=ERROR_COLOR,
        )
        return

    if utilisateur.id == bot.user.id:
        await respond_embed(
            interaction,
            "Interdit",
            "Je ne peux pas me bannir moi-meme.",
            color=ERROR_COLOR,
        )
        return

    bot_member = interaction.guild.me
    if not bot_member:
        await respond_embed(
            interaction,
            "Erreur",
            "Impossible de recuperer mon role.",
            color=ERROR_COLOR,
        )
        return

    if not has_permission(bot_member, "ban_members"):
        await respond_embed(
            interaction,
            "Permission manquante",
            "Il me manque la permission `Ban Members`.",
            color=ERROR_COLOR,
        )
        return

    if bot_member.top_role <= utilisateur.top_role:
        await respond_embed(
            interaction,
            "Hiérarchie",
            "Je ne peux pas bannir cet utilisateur à cause de la hierarchie des roles.",
            color=ERROR_COLOR,
        )
        return

    if interaction.user.top_role <= utilisateur.top_role:
        await respond_embed(
            interaction,
            "Hiérarchie",
            "Tu ne peux pas bannir un membre dont le role est au moins égal au tien.",
            color=ERROR_COLOR,
        )
        return

    try:
        await interaction.guild.ban(utilisateur, reason=raison)
    except discord.Forbidden:
        await respond_embed(
            interaction,
            "Permission refusée",
            "Je n'ai pas la permission de bannir cet utilisateur.",
            color=ERROR_COLOR,
        )
        return

    record_stats_event(interaction.guild.id, "ban")
    await respond_embed(
        interaction,
        "Ban confirmé",
        f"{utilisateur} a été banni.",
        fields=[
            ("Banni par", interaction.user.display_name, True),
            ("Raison", raison, True),
        ],
        footer_text="Ce ban est enregistré côté bot.",
    )
    await send_log(
        interaction.guild,
        f"[BAN] {utilisateur} banni par {interaction.user}. Raison: {raison}",
    )


@bot.tree.command(name="mute", description="Met un membre en timeout")
@staff_only()
@app_commands.describe(
    utilisateur="Membre a mute",
    temps="Duree comme 10m, 2h ou 3d",
    raison="Raison du mute",
)
async def mute_command(
    interaction: discord.Interaction,
    utilisateur: discord.Member,
    temps: str,
    raison: Optional[str] = None,
) -> None:
    assert interaction.guild is not None
    assert isinstance(interaction.user, discord.Member)

    raison = raison or "Aucune raison fournie"

    if utilisateur.id == interaction.user.id:
        await respond_embed(
            interaction,
            "Interdit",
            "Tu ne peux pas te mute toi-meme.",
            color=ERROR_COLOR,
        )
        return

    if utilisateur.id == bot.user.id:
        await respond_embed(
            interaction,
            "Interdit",
            "Je ne peux pas me mute moi-meme.",
            color=ERROR_COLOR,
        )
        return

    bot_member = interaction.guild.me
    if not bot_member:
        await respond_embed(
            interaction,
            "Erreur",
            "Impossible de recuperer mon role.",
            color=ERROR_COLOR,
        )
        return

    if not has_permission(bot_member, "moderate_members"):
        await respond_embed(
            interaction,
            "Permission manquante",
            "Il me manque la permission `Moderate Members`.",
            color=ERROR_COLOR,
        )
        return

    if bot_member.top_role <= utilisateur.top_role:
        await respond_embed(
            interaction,
            "Hiérarchie",
            "Je ne peux pas mute cet utilisateur à cause de la hierarchie des roles.",
            color=ERROR_COLOR,
        )
        return

    if interaction.user.top_role <= utilisateur.top_role:
        await respond_embed(
            interaction,
            "Hiérarchie",
            "Tu ne peux pas mute un membre dont le role est au moins égal au tien.",
            color=ERROR_COLOR,
        )
        return

    try:
        duration = parse_duration(temps)
    except ValueError:
        await respond_embed(
            interaction,
            "Format invalide",
            "Temps invalide. Utilise un format comme 10m, 2h ou 3d.",
            color=ERROR_COLOR,
        )
        return

    await utilisateur.timeout(duration, reason=raison)
    record_stats_event(interaction.guild.id, "mute")
    await respond_embed(
        interaction,
        "Mute appliqué",
        f"{utilisateur} est en timeout.",
        fields=[
            ("Durée", temps, True),
            ("Raison", raison, True),
        ],
        footer_text="La durée est exprimée selon le format que tu as fourni.",
    )
    await send_log(
        interaction.guild,
        f"[MUTE] {utilisateur} mute par {interaction.user} pendant {temps}. Raison: {raison}",
    )


@bot.tree.command(name="envoyer", description="Envoie un message dans un salon")
@staff_only()
@app_commands.describe(
    message="Message a envoyer",
    salon="Salon cible. Si vide, utilise le salon configure ou le salon actuel",
)
async def send_command(
    interaction: discord.Interaction,
    message: str,
    salon: Optional[discord.TextChannel] = None,
) -> None:
    assert interaction.guild is not None

    guild_config = get_guild_config(interaction.guild.id)
    target_channel = salon

    if target_channel is None and guild_config.get("send_channel_id"):
        configured_channel = interaction.guild.get_channel(guild_config["send_channel_id"])
        if isinstance(configured_channel, discord.TextChannel):
            target_channel = configured_channel

    if target_channel is None and isinstance(interaction.channel, discord.TextChannel):
        target_channel = interaction.channel

    if target_channel is None:
        await interaction.response.send_message(
            "Aucun salon texte valide n'a ete trouve.",
            ephemeral=True,
        )
        return

    permissions = target_channel.permissions_for(interaction.guild.me)
    if not permissions.send_messages:
        await interaction.response.send_message(
            "Je ne peux pas envoyer de message dans ce salon.",
            ephemeral=True,
        )
        return

    await target_channel.send(message)
    await respond_embed(
        interaction,
        "Message envoyé",
        "Le message a été posté dans le salon cible.",
        color=INFO_COLOR,
    )


@config_group.command(
    name="staff_role",
    description="Definit le role staff autorise (code requis si deplace)",
)
@app_commands.default_permissions(administrator=True)
@admin_only()
@app_commands.describe(
    role="Role staff qui pourra utiliser les commandes",
    code="Code de validation si le role est deja configure",
)
async def config_staff_role(
    interaction: discord.Interaction, role: discord.Role, code: Optional[str] = None
) -> None:
    assert interaction.guild is not None
    guild_config = get_guild_config(interaction.guild.id)
    existing_role_id = guild_config.get("staff_role_id")
    if existing_role_id and existing_role_id != role.id:
        if code != STAFF_ROLE_LOCK_CODE:
            await respond_embed(
                interaction,
                "Verrou activé",
                "Rôle staff déjà configuré : fourni le code secret pour changer.",
                color=ERROR_COLOR,
            )
            return

    update_guild_config(interaction.guild.id, staff_role_id=role.id)
    await respond_embed(
        interaction,
        "Rôle staff enregistré",
        f"{role.mention} peut désormais utiliser les commandes.",
        fields=[("Code utilisé", "Verrouillage respecté", True)],
    )


@config_group.command(name="log_channel", description="Definit le salon de logs")
@app_commands.default_permissions(administrator=True)
@admin_only()
@app_commands.describe(salon="Salon texte qui recevra les logs")
async def config_log_channel(
    interaction: discord.Interaction, salon: discord.TextChannel
) -> None:
    assert interaction.guild is not None
    update_guild_config(interaction.guild.id, log_channel_id=salon.id)
    await respond_embed(
        interaction,
        "Log configuré",
        f"Les logs vont maintenant dans {salon.mention}.",
    )


@config_group.command(
    name="send_channel", description="Definit le salon par defaut pour /send"
)
@app_commands.default_permissions(administrator=True)
@admin_only()
@app_commands.describe(salon="Salon texte par defaut pour /send")
async def config_send_channel(
    interaction: discord.Interaction, salon: discord.TextChannel
) -> None:
    assert interaction.guild is not None
    update_guild_config(interaction.guild.id, send_channel_id=salon.id)
    await respond_embed(
        interaction,
        "Salon /send enregistré",
        f"Le salon par défaut pour `/send` devient {salon.mention}.",
    )


@config_group.command(name="reset", description="Supprime un parametre de configuration")
@app_commands.default_permissions(administrator=True)
@admin_only()
@app_commands.describe(cle="Parametre a supprimer")
@app_commands.choices(
    cle=[
        app_commands.Choice(name="staff_role", value="staff_role_id"),
        app_commands.Choice(name="log_channel", value="log_channel_id"),
        app_commands.Choice(name="send_channel", value="send_channel_id"),
    ]
)
async def config_reset(
    interaction: discord.Interaction, cle: app_commands.Choice[str]
) -> None:
    assert interaction.guild is not None
    update_guild_config(interaction.guild.id, **{cle.value: None})
    await respond_embed(
        interaction,
        "Configuration purgée",
        f"{cle.name} a été remis à zéro.",
    )


@config_group.command(
    name="openai_reset",
    description="Réactive la connexion OpenAI et réinitialise l'état bloqué",
)
@app_commands.default_permissions(administrator=True)
@admin_only()
async def config_openai_reset(interaction: discord.Interaction) -> None:
    mark_openai_enabled()
    await respond_embed(
        interaction,
        "OpenAI réactivé",
        "L’IA va de nouveau répondre si ton quota le permet.",
        color=INFO_COLOR,
    )


@config_group.command(name="show", description="Affiche la configuration du serveur")
@app_commands.default_permissions(administrator=True)
@admin_only()
async def config_show(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    guild_config = get_guild_config(interaction.guild.id)

    staff_role = (
        interaction.guild.get_role(guild_config["staff_role_id"])
        if guild_config["staff_role_id"]
        else None
    )
    log_channel = (
        interaction.guild.get_channel(guild_config["log_channel_id"])
        if guild_config["log_channel_id"]
        else None
    )
    send_channel = (
        interaction.guild.get_channel(guild_config["send_channel_id"])
        if guild_config["send_channel_id"]
        else None
    )

    openai_state = read_openai_state()
    openai_status = "désactivée" if openai_state.get("disabled") else "activée"
    openai_reason = openai_state.get("reason") or "Pas de verrouillage"

    lines = [
        f"Role staff: {staff_role.mention if isinstance(staff_role, discord.Role) else 'non defini'}",
        f"Salon logs: {log_channel.mention if isinstance(log_channel, discord.TextChannel) else 'non defini'}",
        f"Salon /send par defaut: {send_channel.mention if isinstance(send_channel, discord.TextChannel) else 'non defini'}",
    ]

    await respond_embed(
        interaction,
        "Configuration actuelle",
        "Voici les paramètres actifs sur ce serveur :",
        fields=[
            ("Role staff", staff_role.mention if isinstance(staff_role, discord.Role) else "non défini", True),
            ("Salon logs", log_channel.mention if isinstance(log_channel, discord.TextChannel) else "non défini", True),
            ("Salon /send", send_channel.mention if isinstance(send_channel, discord.TextChannel) else "non défini", True),
            ("Statut OpenAI", f"{openai_status} ({openai_reason})", True),
        ],
    )


@bot.tree.command(name="stats", description="Affiche l'évolution du serveur en violet et noir")
async def stats_command(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(thinking=True)
    chart_path = await build_stats_chart(interaction.guild.id)
    try:
        file = discord.File(chart_path, filename="server-stats.png")
        await interaction.followup.send(
            "Voici l'évolution des bans/mutes/tickets sur 7 jours :", file=file
        )
    finally:
        try:
            Path(chart_path).unlink()
        except Exception:
            pass


@bot.tree.command(
    name="herbo_recap",
    description="Envoie un récap hebdo manuel via le salon de logs",
)
@staff_only()
async def herbo_recap(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(thinking=True)
    await send_recap_to_guild(interaction.guild, triggered_by=interaction.user)
    await interaction.followup.send("Récap hebdo envoyé dans le salon de logs.", ephemeral=True)


@ticket_group.command(
    name="category",
    description="Définit la catégorie à surveiller pour les tickets",
)
@app_commands.default_permissions(administrator=True)
@admin_only()
@app_commands.describe(category="Catégorie qui contiendra les tickets")
async def ticket_category(
    interaction: discord.Interaction, category: discord.CategoryChannel
) -> None:
    assert interaction.guild is not None
    update_guild_config(interaction.guild.id, ticket_category_id=category.id)
    await respond_embed(
        interaction,
        "Catégorie enregistrée",
        f"Tout salon créé dans {category.name} sera considéré comme un ticket et recevra un welcome message.",
    )


@ticket_group.command(
    name="roles",
    description="Configure les rôles mentionnés par les boutons du ticket",
)
@app_commands.default_permissions(administrator=True)
@admin_only()
@app_commands.describe(
    fondateur="Rôle utilisé pour la mention Fondateur",
    cofounder="Rôle utilisé pour la mention Co-fonda",
)
async def ticket_roles(
    interaction: discord.Interaction,
    fondateur: discord.Role,
    cofounder: discord.Role,
) -> None:
    assert interaction.guild is not None
    update_guild_config(
        interaction.guild.id,
        ticket_founder_role_id=fondateur.id,
        ticket_cofounder_role_id=cofounder.id,
    )
    await respond_embed(
        interaction,
        "Rôles tickets",
        "Les boutons Fondateur et Co-fonda mentionneront désormais les bons rôles.",
        fields=[
            ("Fondateur", fondateur.mention, True),
            ("Co-fonda", cofounder.mention, True),
        ],
    )


@ticket_group.command(
    name="reset",
    description="Réactive la réponse automatique pour un ticket donné",
)
@app_commands.default_permissions(administrator=True)
@admin_only()
@app_commands.describe(salon="Salon cible (optionnel)")
async def ticket_reset(
    interaction: discord.Interaction, salon: Optional[discord.TextChannel] = None
) -> None:
    target = salon or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await respond_embed(
            interaction,
            "Erreur",
            "Choisis un salon texte valide.",
            color=ERROR_COLOR,
        )
        return

    ticket_ai_responded.discard(target.id)
    await respond_embed(
        interaction,
        "Ticket remis à zéro",
        f"{target.mention} peut recevoir de nouveau une première réponse IA/local.",
        color=INFO_COLOR,
    )


bot.tree.add_command(config_group)
bot.tree.add_command(ticket_group)
bot.run(TOKEN)
