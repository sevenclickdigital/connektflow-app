"""Brazilian holidays and commercial dates shown on the calendar and on the idea board.

Fixed dates repeat on the same day every year; the rest move (Carnival and
Corpus Christi follow Easter, Mother's Day is the second Sunday of May, Black
Friday the day after the fourth Thursday of November) and are computed per year.
Names are translatable source strings, so they render in the user's language.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from django.utils.translation import gettext_lazy as _

HOLIDAY = "holiday"
COMMERCIAL = "commercial"

# Content ideas offered for a date that brings none of its own: the title of
# the idea and the brief that opens in its description, telling the brand how
# to develop it.
_OFFER = (
    _("Special offer for the date"),
    _(
        "Announce one clear offer for the date: what is on offer, the price or discount, and until when. "
        "Show the product in use and end with how to buy (link, WhatsApp or store). "
        "Ideas: a carousel with the items on offer, a short video of the product, a reminder in Stories on the last day."
    ),
)
_COUNTDOWN = (
    _("Countdown in Stories"),
    _(
        "Post one Story a day in the days before the date, each with one product or one reason to buy, "
        "and add the countdown sticker so people get a reminder. "
        "On the last day, show what is still available and how to order."
    ),
)
_GUIDE = (
    _("Gift or product guide"),
    _(
        "Put together a selection of products for the date, grouped by who they are for or by price range. "
        "One slide per suggestion, with photo, name and price, and how to order at the end."
    ),
)
_TESTIMONIAL = (
    _("Customer testimonial"),
    _(
        "Share what a customer said about buying from you around this date: their words, a photo or a short video, "
        "with their permission. Say which product or service it was and invite others to tell their story."
    ),
)
_SEASON_TIPS = (
    _("Tips for the season"),
    _(
        "Teach something useful for this time of year that has to do with what you sell: "
        "three to five short tips, one per slide or per scene. End by showing how your product or service helps."
    ),
)
_FITTING_PRODUCTS = (
    _("Products or services that fit the moment"),
    _(
        "Show what in your catalogue makes the most sense now, and why. Explain the situation it solves "
        "at this time of year, with photos of real use, and say how to buy or book."
    ),
)
_BACKSTAGE = (
    _("Behind the scenes of the preparation"),
    _(
        "Show how the brand gets ready for the date: the team, the production, the packaging, the decoration. "
        "Short videos or photos with little editing work well, and bring people closer to who is behind the brand."
    ),
)
_MESSAGE = (
    _("Message from the brand for the date"),
    _(
        "Write a short, sincere message about what the date means, in the brand's own tone of voice. "
        "A photo of the team or an image in the brand's colours goes well with it. Avoid selling in this post."
    ),
)
_FACT = (
    _("A fact about the date"),
    _(
        "Tell something few people know about the date: its origin, a number or a tradition. "
        "Link it to your line of business where it fits naturally, "
        "and close with a question to start a conversation in the comments."
    ),
)
_OPENING_HOURS = (
    _("Opening hours on the holiday"),
    _(
        "Tell your customers whether you open on the holiday and at what times, and how to reach you meanwhile "
        "(delivery, WhatsApp, online orders). Post a day or two before and repeat it in Stories on the day."
    ),
)
_MEANING = (
    _("What the date means to your brand"),
    _(
        "Explain why the brand cares about this date: a story, a value you share or something you do in practice. "
        "Concrete examples are worth more than slogans."
    ),
)
_TAKE_PART = (
    _("Invite your audience to take part"),
    _(
        "Ask people to join in: answer a question, share a photo, tag someone or use a hashtag. "
        "Say exactly what to do, and repost the best answers in Stories."
    ),
)

# By category (``SpecialDate.Kind``; a built-in holiday counts as national).
GENERIC_SUGGESTIONS = {
    "commercial": (_OFFER, _COUNTDOWN, _GUIDE, _TESTIMONIAL),
    "seasonal": (_SEASON_TIPS, _FITTING_PRODUCTS, _BACKSTAGE),
    "national": (_MESSAGE, _FACT, _BACKSTAGE),
    # Not a category: the built-in public holidays, where the shop may close.
    HOLIDAY: (_MESSAGE, _OPENING_HOURS, _FACT),
    "international": (_MEANING, _FACT, _TAKE_PART),
}


def suggestions_for(kind: str) -> list[dict]:
    """``[{"title", "brief"}]`` for a category, in the user's language."""
    return [{"title": str(title), "brief": str(brief)} for title, brief in GENERIC_SUGGESTIONS.get(kind, ())]


def _own_suggestions(row) -> list[dict]:
    """The operator's suggestions for a date: one per line, as ``title`` or
    ``title | brief``."""
    result = []
    for line in row.suggestion_list:
        title, _sep, brief = line.partition("|")
        if title.strip():
            result.append({"title": title.strip(), "brief": brief.strip()})
    return result


# (month, day, slug, name, emoji, kind). The slug names the date for good:
# it keys ``BUILT_IN_CONTENT`` and the brands' favorites.
FIXED = (
    (1, 1, "new-year", _("New Year's Day"), "🎉", HOLIDAY),
    (3, 8, "womens-day", _("International Women's Day"), "💜", COMMERCIAL),
    (3, 15, "consumer-day", _("Consumer Day"), "🛍️", COMMERCIAL),
    (4, 21, "tiradentes", _("Tiradentes Day"), "🇧🇷", HOLIDAY),
    (5, 1, "labour-day", _("Labour Day"), "🛠️", HOLIDAY),
    (6, 12, "valentines", _("Valentine's Day"), "❤️", COMMERCIAL),
    (6, 24, "saint-john", _("Saint John's Day (Festa Junina)"), "🌽", COMMERCIAL),
    (9, 7, "independence", _("Independence Day"), "🇧🇷", HOLIDAY),
    (9, 15, "customer-day", _("Customer Day"), "🤝", COMMERCIAL),
    (10, 12, "childrens-day", _("Children's Day"), "🧸", HOLIDAY),
    (10, 15, "teachers-day", _("Teachers' Day"), "🍎", COMMERCIAL),
    (10, 31, "halloween", _("Halloween"), "🎃", COMMERCIAL),
    (11, 2, "all-souls", _("All Souls' Day"), "🕯️", HOLIDAY),
    (11, 15, "republic", _("Proclamation of the Republic"), "🇧🇷", HOLIDAY),
    (11, 20, "black-consciousness", _("Black Consciousness Day"), "✊🏿", HOLIDAY),
    (12, 25, "christmas", _("Christmas"), "🎄", HOLIDAY),
    (12, 31, "new-years-eve", _("New Year's Eve"), "🥂", COMMERCIAL),
)

# The icon each built-in date is drawn with: slug → a slug of the app's icon
# library (``apps.composer.idea_icons.ICONS``).
BUILT_IN_ICONS = {
    "new-year": "star",
    "womens-day": "heart",
    "consumer-day": "shopping-bag",
    "tiradentes": "flag",
    "labour-day": "tools",
    "valentines": "heart",
    "saint-john": "flame",
    "independence": "flag",
    "customer-day": "users",
    "childrens-day": "smile",
    "teachers-day": "book",
    "halloween": "zap",
    "all-souls": "flame",
    "republic": "flag",
    "black-consciousness": "users",
    "christmas": "gift",
    "new-years-eve": "clock",
    "carnival": "smile",
    "good-friday": "calendar",
    "easter": "gift",
    "mothers-day": "heart",
    "corpus-christi": "calendar",
    "fathers-day": "award",
    "black-friday": "tag",
}

# The icon of a date that has none of its own (the operator's dates), by
# category (``SpecialDate.Kind``).
KIND_ICONS = {
    "commercial": "shopping-bag",
    "seasonal": "clock",
    "national": "flag",
    "international": "globe",
}
DEFAULT_DATE_ICON = "calendar"

# What the special-dates tab says about each built-in date: slug →
# (description, tags as one comma-separated string). Both are translated.
BUILT_IN_CONTENT = {
    "new-year": (
        _("The first day of the year: a moment for fresh starts, goals and good wishes to your audience."),
        _("newyear, goals, freshstart"),
    ),
    "womens-day": (
        _(
            "A date to recognise women and their achievements. Speak with respect, and show the women behind your brand."
        ),
        _("womensday, women, respect"),
    ),
    "consumer-day": (
        _("One of the strongest sales dates of the first half of the year, with offers across retail and services."),
        _("consumerday, offers, discount"),
    ),
    "tiradentes": (
        _("National holiday in memory of Tiradentes. Many businesses close or change their hours."),
        _("tiradentes, holiday"),
    ),
    "labour-day": (
        _("National holiday that honours workers. A good moment to thank your team and show who makes the brand."),
        _("labourday, team, holiday"),
    ),
    "valentines": (
        _("Brazil's day for couples, on 12 June. Gifts, experiences and dinners sell well in the weeks before."),
        _("valentinesday, love, gifts"),
    ),
    "saint-john": (
        _("The heart of the June festivals: bonfires, typical food and country-style parties across Brazil."),
        _("festajunina, saintjohn, typicalfood"),
    ),
    "independence": (
        _("National holiday for Brazil's independence, on 7 September. Green and yellow set the tone."),
        _("independence, brazil, holiday"),
    ),
    "customer-day": (
        _("A date to thank the people who choose your brand, with a message, a benefit or a special condition."),
        _("customerday, thankyou, loyalty"),
    ),
    "childrens-day": (
        _("One of the main gift dates of the year, on the same day as the holiday of Our Lady of Aparecida."),
        _("childrensday, kids, gifts"),
    ),
    "teachers-day": (
        _("A date to thank teachers and everyone who teaches. Works well with stories about who taught you."),
        _("teachersday, education, thankyou"),
    ),
    "halloween": (
        _("Costumes, sweets and playful scares on 31 October. Themed content and offers get attention."),
        _("halloween, costume, sweets"),
    ),
    "all-souls": (
        _("A day of remembrance and a national holiday. The tone is quiet and respectful; avoid sales messages."),
        _("allsouls, remembrance, holiday"),
    ),
    "republic": (
        _("National holiday for the Proclamation of the Republic, on 15 November."),
        _("republic, brazil, holiday"),
    ),
    "black-consciousness": (
        _("A date of respect, equality and appreciation of Afro-Brazilian culture, and a national holiday."),
        _("blackconsciousness, equality, culture"),
    ),
    "christmas": (
        _("A time of togetherness and the biggest gift season of the year. Campaigns start weeks before."),
        _("christmas, gifts, family"),
    ),
    "new-years-eve": (
        _("The last night of the year: a look back, thanks to customers and wishes for the year ahead."),
        _("newyearseve, lookback, celebration"),
    ),
    "carnival": (
        _("Brazil's biggest street party. Many businesses change their hours, and colourful content travels far."),
        _("carnival, party, holiday"),
    ),
    "good-friday": (
        _("A religious holiday before Easter. The tone is calm; many businesses close for the day."),
        _("goodfriday, easter, holiday"),
    ),
    "easter": (
        _("Chocolate, family lunches and renewal. Sales of gifts and food grow in the weeks before."),
        _("easter, chocolate, family"),
    ),
    "mothers-day": (
        _("The second biggest retail date of the year in Brazil, on the second Sunday of May."),
        _("mothersday, mother, gifts"),
    ),
    "corpus-christi": (
        _("A religious date sixty days after Easter, a day off in much of the country."),
        _("corpuschristi, holiday"),
    ),
    "fathers-day": (
        _("On the second Sunday of August: gifts, tributes and stories about fathers."),
        _("fathersday, father, gifts"),
    ),
    "black-friday": (
        _("One of the biggest chances of the year to sell more, with special conditions, discounts and campaigns."),
        _("blackfriday, sales, discount, promotion"),
    ),
}

# What a brand's task for a built-in date asks the creative team for ("Create task"):
# slug → (briefing, ((format, count, note), …)), formats of apps.tasks.creative_formats.
# The operator's dates carry the same in SpecialDate.briefing / art_items.
BUILT_IN_ARTS: dict = {
    "new-year": (
        _(
            "Open the year with a fresh-start message from the brand. The feed post puts a short wish in "
            "large type over a clean, light background, with the year's number as the main visual element and"
            " the logo small at the bottom. The story repeats the wish and invites followers to share one "
            "goal with the question sticker. The Reels shows quick clips of the team wishing everyone a good "
            "year and ends on the same wish as the post."
        ),
        (
            ("ig_feed_portrait", 1, _("The year's number large, short wish below, logo small at the bottom")),
            ("ig_story", 2, _("First with the wish; second with a question sticker asking for a goal")),
            ("ig_reel", 1, _("Team clips wishing a good year, ending on the post's wish")),
            ("li_post_square", 1, _("Same art as the feed post, adapted to the square")),
        ),
    ),
    "carnival": (
        _(
            "Celebrate Carnival with colour, but give the practical information first. The main post is a "
            "carousel: a festive cover with confetti and streamers, then the opening hours for each day of "
            "the holiday in large, easy-to-read type, ending with a see-you-after-the-party message. Use the "
            "brand's own colours turned up, not generic stock images. The story highlights the changed hours,"
            " and the Reels shows the team in light costumes or the products dressed up for the party."
        ),
        (
            (
                "ig_feed_portrait",
                4,
                _("Festive cover; holiday hours; contact channels; last with the see-you-after-the-party farewell"),
            ),
            ("ig_story", 2, _("Changed hours in large type; countdown sticker to reopening")),
            ("ig_reel", 1, _("Team in light costumes or products dressed up, fast cuts in a carnival mood")),
            ("fb_post_square", 1, _("The holiday hours slide adapted to the square")),
        ),
    ),
    "womens-day": (
        _(
            "Honour women with real faces, not flowers or discounts. The carousel presents the women who "
            "build the brand: a portrait of each one with her name, role and one sentence in her own words, "
            "after a cover that names the date with restraint. Keep the design clean, with the photos as the "
            "main element and little text. The Reels shows these women talking to camera about their work, "
            "and the story invites followers to tag a woman who inspires them. No sales message."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Restrained cover; one woman per slide with photo, name and quote; last with a thank-you"),
            ),
            ("ig_reel", 1, _("Short testimonies to camera from the women on the team")),
            ("ig_story", 1, _("Invitation to tag a woman who inspires you, with the mention sticker")),
            ("li_post_square", 1, _("Group photo of the women on the team with a short tribute")),
        ),
    ),
    "consumer-day": (
        _(
            "Sell with one clear offer on Consumer Day. The discount or condition is the hero of every piece:"
            " the number in very large type, the product photo beside it and the validity period small but "
            "visible. The carousel shows the products on offer, one per slide, with the old and the new "
            "price; the stories create urgency with a countdown; the Reels shows the products at a fast pace "
            "with the offer on screen. Close every piece with how to buy."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Cover with the offer; one product per slide with old and new price; last with how to buy"),
            ),
            ("ig_story", 3, _("Offer announcement; countdown sticker; last-day reminder with link")),
            ("ig_reel", 1, _("Products in quick cuts with the discount on screen")),
            ("fb_post_square", 1, _("The carousel cover adapted to the square")),
        ),
    ),
    "good-friday": (
        _(
            "Mark Good Friday with a calm, reflective message and a practical note about opening hours. The "
            "post uses soft, neutral colours, one short line of reflection in elegant type and a discreet "
            "symbolic image, such as natural light or bread, with the brand's logo small. No offers or sales "
            "language. The story says whether the business is closed or open with different hours."
        ),
        (
            ("ig_feed_square", 1, _("Short reflection over a soft image, logo small")),
            ("ig_story", 1, _("Holiday hours notice on a neutral background")),
            ("fb_post_square", 1, _("Same art as the feed post")),
        ),
    ),
    "easter": (
        _(
            "Sell Easter through family moments and the products that fit them. The carousel works as a gift "
            "or menu guide: a cover with eggs or chocolate in a warm photo, then one suggestion per slide "
            "with photo, short description and price, ending with the order deadline. The story highlights "
            "the deadline to order before the holiday, and the Reels shows the products being made or wrapped"
            " in an Easter mood. Warm, soft colours; the product photos are the main element."
        ),
        (
            ("ig_feed_portrait", 6, _("Warm cover; one product per slide with price; last with the order deadline")),
            ("ig_story", 2, _("Order deadline in large type; link or contact to order")),
            ("ig_reel", 1, _("Products being made or wrapped, ending on the deadline")),
            ("pin_standard", 2, _("Gift or recipe ideas with the product photo, for search")),
        ),
    ),
    "tiradentes": (
        _(
            "Mark Tiradentes Day with a sober civic message and the holiday notice. The post shows the date "
            "and the name in clear type over a solid background in muted tones, with a short line about "
            "freedom and the memory of the Inconfidência Mineira, and no offers. The story tells followers "
            "the business's hours on the holiday."
        ),
        (
            ("ig_feed_square", 1, _("Date and name in clear type, short line about freedom")),
            ("ig_story", 1, _("Holiday hours notice")),
            ("fb_post_square", 1, _("Same art as the feed post")),
        ),
    ),
    "labour-day": (
        _(
            "Use Labour Day to thank the team and show the people behind the brand. The carousel presents "
            "team members at work in real photos, with name and role on each slide, opening with a thank-you "
            "cover and ending with the holiday hours. No sales message. The Reels shows a quick sequence of "
            "the team's daily routine, and the LinkedIn post adapts the thank-you for a professional "
            "audience."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Thank-you cover; one team member per slide at work; last with the holiday hours"),
            ),
            ("ig_reel", 1, _("Quick sequence of the team's routine, ending on the thank-you")),
            ("ig_story", 1, _("Holiday hours notice")),
            ("li_post_square", 1, _("Team photo with the thank-you message")),
        ),
    ),
    "mothers-day": (
        _(
            "Sell Mother's Day with emotion and clear gift suggestions. The main carousel is a gift guide by "
            "type of mother, with product photo, short description and price on each slide, after an "
            "affectionate cover. The Reels can bring customers or team members talking about their mothers, "
            "ending on the gift guide. The stories push the delivery or purchase deadline before the second "
            "Sunday of May. Warm palette, real photos, the product always visible."
        ),
        (
            (
                "ig_feed_portrait",
                7,
                _("Affectionate cover; one type of mother and gift per slide; last with how to buy"),
            ),
            ("ig_reel", 1, _("People talking about their mothers, ending on the gift guide")),
            ("ig_story", 3, _("Gift suggestion with link; deadline countdown; last call")),
            ("fb_post_square", 1, _("The carousel cover adapted to the square")),
        ),
    ),
    "corpus-christi": (
        _(
            "Mark Corpus Christi with a quiet message and a practical note about the day off. The post uses a"
            " soft image, such as the coloured carpets of the processions, with one short, respectful line "
            "and the logo small. No offers. The story says whether the business opens on the holiday and on "
            "the bridge day, if there is one."
        ),
        (
            ("ig_feed_square", 1, _("Soft image of the procession carpets with a short line")),
            ("ig_story", 1, _("Hours on the holiday and the bridge day")),
            ("fb_post_square", 1, _("Same art as the feed post")),
        ),
    ),
    "valentines": (
        _(
            "Sell Valentine's Day (12 June) as an experience for couples. The carousel presents gift or "
            "experience bundles for two, one per slide, with a staged photo for two, price and a short "
            "romantic line, after a cover with the date. The Reels shows a gift or dinner being prepared to a"
            " romantic soundtrack, and the stories collect couples' stories with the question sticker and "
            "push the order deadline. Reds and pinks adjusted to the brand's colours."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Cover with the date; one bundle for two per slide with price; last with how to order"),
            ),
            ("ig_reel", 1, _("A gift or dinner being prepared, romantic soundtrack")),
            ("ig_story", 3, _("Question sticker for couples' stories; best bundle with link; order deadline")),
            ("fb_post_square", 1, _("The carousel cover adapted to the square")),
        ),
    ),
    "saint-john": (
        _(
            "Bring the Festa Junina mood into the brand with bunting, bonfire, checked fabric and typical "
            "food. The carousel can be a themed menu or product list, one item per slide with a photo, after "
            "a cover showing the brand's own arraial. The Reels shows typical dishes or the team in country-"
            "style outfits, and the stories play with a junina quiz or a poll. Warm, rustic colours (straw, "
            "red, orange) and hand-drawn-style type."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Arraial cover with bunting; one themed item per slide with photo; last with how to order"),
            ),
            ("ig_reel", 1, _("Typical dishes or the team in country-style outfits")),
            ("ig_story", 3, _("Junina quiz; poll; themed offer or menu with link")),
            ("tt_video", 1, _("Same video as the Reels")),
        ),
    ),
    "fathers-day": (
        _(
            "Sell Father's Day with stories and practical gift ideas. The carousel is a gift guide by type of"
            " father, one product per slide with photo, short description and price, after a cover that "
            "speaks to the son or daughter choosing the gift. The Reels brings team members or customers "
            "telling a short story about their fathers, and the stories remind followers of the purchase "
            "deadline before the second Sunday of August. Sober, warm colours; real photos."
        ),
        (
            (
                "ig_feed_portrait",
                7,
                _("Cover speaking to the buyer; one type of father and gift per slide; last with how to buy"),
            ),
            ("ig_reel", 1, _("Short stories about fathers told to camera, ending on the gift guide")),
            ("ig_story", 3, _("Gift suggestion with link; deadline countdown; last call")),
            ("fb_post_square", 1, _("The carousel cover adapted to the square")),
        ),
    ),
    "independence": (
        _(
            "Mark Independence Day (7 September) with a civic tone and the national colours. The post shows "
            "the date in large type, with green and yellow applied to the brand's identity and a short "
            "message about Brazil, and no offers. The story gives the holiday hours. If the brand wants, a "
            "light Reels can show Brazilian elements of its own product or place."
        ),
        (
            ("ig_feed_square", 1, _("Date in large type in green and yellow, short message about Brazil")),
            ("ig_story", 1, _("Holiday hours notice")),
            ("ig_reel", 1, _("Brazilian elements of the product or place, light tone")),
            ("fb_post_square", 1, _("Same art as the feed post")),
        ),
    ),
    "customer-day": (
        _(
            "Thank customers personally and give them something on Customer Day. The carousel opens with a "
            "big thank-you, then shows real customer photos or testimonials (with permission), ending with "
            "the benefit offered for the date and how to use it. The Reels can be the team saying thank you "
            "to camera, and the stories announce the benefit with a link or coupon. Warm, personal tone; the "
            "customers are the main element, not the products."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Big thank-you on the cover; one customer or testimony per slide; last with the benefit"),
            ),
            ("ig_reel", 1, _("The team saying thank you to camera")),
            ("ig_story", 2, _("Benefit announcement; coupon or link to use it")),
            ("fb_post_square", 1, _("The carousel cover adapted to the square")),
        ),
    ),
    "childrens-day": (
        _(
            "Sell Children's Day with play and colour, speaking to the parents who buy. The carousel is a "
            "gift guide by age group, one suggestion per slide with photo and price, after a playful "
            "illustrated cover. The Reels shows children (with authorisation) or the products in a playful "
            "way, and the stories bring a poll and the order deadline. Bright colours and rounded type. Since"
            " the date shares the day with the Our Lady of Aparecida holiday, give the hours in the stories."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Playful illustrated cover; one age group and gift per slide; last with how to buy"),
            ),
            ("ig_reel", 1, _("Children or products shown in a playful way")),
            ("ig_story", 3, _("Gift poll; order deadline; holiday hours")),
            ("fb_post_square", 1, _("The carousel cover adapted to the square")),
        ),
    ),
    "teachers-day": (
        _(
            "Thank teachers through real stories of who taught us. The carousel opens with the question "
            "'Which teacher made a difference in your life?' in large type and shows short testimonies from "
            "the team, each with a photo and the teacher's name, ending with an invitation for followers to "
            "comment. The Reels brings these testimonies to camera, and the LinkedIn post adapts the tribute."
            " Sober, warm tone, with a notebook or blackboard texture as background."
        ),
        (
            ("ig_feed_portrait", 5, _("The question on the cover; one testimony per slide; last inviting comments")),
            ("ig_reel", 1, _("Team members talking to camera about a teacher who marked them")),
            ("ig_story", 1, _("Question sticker asking followers about their teacher")),
            ("li_post_square", 1, _("Tribute to teachers with one testimony from the team")),
        ),
    ),
    "halloween": (
        _(
            "Use Halloween with humour and playful scares, never gore. The carousel shows the products in "
            "costume or in a themed setting with pumpkins, cobwebs and sweets, with the offer or themed menu "
            "in large type. The Reels plays a light scare or a transformation with the product, and the "
            "stories bring a trick-or-treat quiz with a coupon as the treat. Black, purple and orange adapted"
            " to the brand."
        ),
        (
            (
                "ig_feed_portrait",
                5,
                _("Themed cover with the offer; one product in costume per slide; last with how to buy"),
            ),
            ("ig_reel", 1, _("Light scare or product transformation")),
            ("ig_story", 3, _("Trick-or-treat quiz; coupon reveal; last day of the offer")),
            ("tt_video", 1, _("Same video as the Reels")),
        ),
    ),
    "all-souls": (
        _(
            "Mark All Souls' Day with silence and respect. A single post with a short line of remembrance in "
            "delicate type over a calm image, such as a flower or soft light, in muted tones, with the logo "
            "very small or left out. No offers, no promotional language and no humour. The story only gives "
            "the holiday hours, in the same visual tone."
        ),
        (
            ("ig_feed_square", 1, _("Short line of remembrance over a calm image, muted tones")),
            ("ig_story", 1, _("Holiday hours only, same visual tone")),
            ("fb_post_square", 1, _("Same art as the feed post")),
        ),
    ),
    "republic": (
        _(
            "Mark the Proclamation of the Republic (15 November) with a short, sober civic message. The post "
            "shows the date and the name in clear type over a solid background with discreet national colours"
            " and a line about citizenship, and no offers. The story gives the holiday hours."
        ),
        (
            ("ig_feed_square", 1, _("Date and name in clear type, line about citizenship")),
            ("ig_story", 1, _("Holiday hours notice")),
            ("fb_post_square", 1, _("Same art as the feed post")),
        ),
    ),
    "black-consciousness": (
        _(
            "Mark Black Consciousness Day (20 November) with respect and real content, not promotion. The "
            "carousel presents Black people from the team, or Afro-Brazilian figures and references connected"
            " to the brand's field, with photo, name and a short sentence, after a cover that names the date "
            "and the legacy of Zumbi dos Palmares. The Reels gives voice to team members talking about "
            "representation. No offers; the design leaves room for faces and words."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Cover with the date and Zumbi's legacy; one person or reference per slide; last with a reflection"),
            ),
            ("ig_reel", 1, _("Team members talking to camera about representation")),
            ("ig_story", 1, _("Holiday hours in the same sober tone")),
            ("li_post_square", 1, _("Carousel cover adapted, with a short reflection")),
        ),
    ),
    "black-friday": (
        _(
            "Sell hard on Black Friday with the discount as the hero. Every piece uses a black background "
            "with the brand's accent colour, the percentage or price in very large type and the product photo"
            " cut out. The carousel lists the best offers, one per slide, with old and new price; the stories"
            " run a countdown and reveal new offers each day; the Reels shows the products in fast cuts with "
            "the prices on screen. Always state the validity and how to buy."
        ),
        (
            (
                "ig_feed_portrait",
                7,
                _("Cover with the biggest discount; one offer per slide with old and new price; last with how to buy"),
            ),
            ("ig_story", 5, _("Countdown; one new offer per day with link; last-hours call")),
            ("ig_reel", 1, _("Products in fast cuts with prices on screen")),
            ("fb_post_square", 2, _("The two strongest offers adapted to the square")),
            ("tt_video", 1, _("Same video as the Reels")),
        ),
    ),
    "christmas": (
        _(
            "Build Christmas as a campaign that starts weeks ahead. The carousel is a gift guide by budget, "
            "one product per slide with photo and price, after a cover with a warm Christmas setting, lights "
            "and the brand's packaging. The Reels shows gifts being wrapped or the team's Christmas message, "
            "and the stories push the delivery deadline before 25 December. Warm, festive palette (red, "
            "green, gold) adapted to the brand."
        ),
        (
            (
                "ig_feed_portrait",
                7,
                _("Christmas cover; one price range and gift per slide; last with the delivery deadline"),
            ),
            ("ig_reel", 1, _("Gifts being wrapped or the team's Christmas message")),
            ("ig_story", 3, _("Gift suggestion with link; delivery deadline; Christmas Eve and Day hours")),
            ("fb_post_square", 1, _("The carousel cover adapted to the square")),
            ("pin_standard", 3, _("One gift idea per pin with product photo and price range")),
        ),
    ),
    "new-years-eve": (
        _(
            "Close the year by looking back and thanking customers. The carousel shows the year's highlights "
            "in numbers and photos (launches, milestones, customers served), one per slide, ending with a "
            "thank-you and a wish for the new year. The Reels condenses the year into a fast montage of "
            "photos and videos, and the stories give the hours for 31 December and 1 January. Elegant, "
            "festive palette with gold and white."
        ),
        (
            (
                "ig_feed_portrait",
                6,
                _("Year-in-review cover; one highlight per slide in numbers or photos; last with thanks and a wish"),
            ),
            ("ig_reel", 1, _("Fast montage of the year's photos and videos")),
            ("ig_story", 2, _("Hours on 31 December and 1 January; thank-you to customers")),
            ("li_post_square", 1, _("The year's main numbers with a thank-you")),
        ),
    ),
}


def _built_in_arts(slug) -> tuple[str, list[dict]]:
    briefing, items = BUILT_IN_ARTS.get(slug, ("", ()))
    return str(briefing), [{"format": key, "count": count, "note": str(note)} for key, count, note in items]


def easter(year: int) -> date:
    """Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Monday=0) of a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _movable(year: int):
    easter_day = easter(year)
    return (
        (easter_day - timedelta(days=47), "carnival", _("Carnival"), "🎭", HOLIDAY),
        (easter_day - timedelta(days=2), "good-friday", _("Good Friday"), "✝️", HOLIDAY),
        (easter_day, "easter", _("Easter"), "🐣", COMMERCIAL),
        (nth_weekday(year, 5, 6, 2), "mothers-day", _("Mother's Day"), "💐", COMMERCIAL),
        (easter_day + timedelta(days=60), "corpus-christi", _("Corpus Christi"), "⛪", HOLIDAY),
        (nth_weekday(year, 8, 6, 2), "fathers-day", _("Father's Day"), "👔", COMMERCIAL),
        (nth_weekday(year, 11, 3, 4) + timedelta(days=1), "black-friday", _("Black Friday"), "🏷️", COMMERCIAL),
    )


@lru_cache(maxsize=16)
def _year(year: int) -> tuple:
    entries = [(date(year, m, d), slug, name, emoji, kind) for m, d, slug, name, emoji, kind in FIXED]
    entries.extend(_movable(year))
    return tuple(sorted(entries, key=lambda e: e[0]))


def _tag_list(text) -> list[str]:
    return [tag.strip() for tag in str(text).split(",") if tag.strip()]


def _managed_between(start_date, end_date, workspace) -> list[dict]:
    """The operator's dates in the range: the global ones, plus those of the
    segments ``workspace`` follows. A day that does not exist in a year
    (29 February) is skipped that year."""
    from django.db.models import Q

    from .models import SpecialDate

    wanted = Q(segment__isnull=True)
    if workspace is not None:
        wanted |= Q(segment__is_active=True, segment__workspace_choices__workspace=workspace)
    result = []
    rows = SpecialDate.objects.filter(wanted, is_active=True).select_related("segment").distinct()
    for row in rows:
        for year in range(start_date.year, end_date.year + 1):
            try:
                day = date(year, row.month, row.day)
            except ValueError:
                continue
            if start_date <= day <= end_date:
                result.append(
                    {
                        "date": day,
                        "name": row.name,
                        "emoji": row.emoji,
                        "icon": KIND_ICONS.get(row.kind, DEFAULT_DATE_ICON),
                        "type": COMMERCIAL,
                        "segment": row.segment.name if row.segment else "",
                        "built_in": False,
                        "key": f"d:{row.id}",
                        "image": row.image.url if row.image else "",
                        "kind": row.kind,
                        "description": row.description,
                        "suggestions": _own_suggestions(row) or suggestions_for(row.kind),
                        "tags": row.tag_list,
                        "briefing": row.briefing,
                        "art_items": row.art_items,
                    }
                )
    return result


def special_dates_between(start_date, end_date, workspace=None) -> list[dict]:
    """``[{"date", "name", "emoji", "icon", "type", "segment", "built_in",
    "key", "image", "kind", "description", "suggestions", "tags", "briefing",
    "art_items"}]`` from
    ``start_date`` to ``end_date`` inclusive, by date.

    Global dates (the built-in ones and those the operator added without a
    segment) always come; with ``workspace``, so do the dates of the segments
    it follows, each carrying its segment's name (``""`` for a global one).
    ``built_in`` tells the dates in this module from the operator's.
    ``type`` is what the calendar grid styles by (holiday / commercial);
    ``kind`` is the category of the special-dates tab (``SpecialDate.Kind``).
    ``suggestions`` is ``[{"title", "brief"}]``.
    ``emoji`` is what the calendar grid prints; ``icon`` (a slug of the icon
    library) is what the idea hub draws.
    """
    result = []
    for year in range(start_date.year, end_date.year + 1):
        for day, slug, name, emoji, kind in _year(year):
            if start_date <= day <= end_date:
                description, tags = BUILT_IN_CONTENT.get(slug, ("", ""))
                briefing, art_items = _built_in_arts(slug)
                result.append(
                    {
                        "date": day,
                        "name": str(name),
                        "emoji": emoji,
                        "icon": BUILT_IN_ICONS.get(slug, DEFAULT_DATE_ICON),
                        "type": kind,
                        "segment": "",
                        "built_in": True,
                        "key": f"b:{slug}",
                        "image": "",
                        "kind": "national" if kind == HOLIDAY else "commercial",
                        "description": str(description),
                        "suggestions": suggestions_for(kind),
                        "tags": _tag_list(tags),
                        "briefing": briefing,
                        "art_items": art_items,
                    }
                )
    result.extend(_managed_between(start_date, end_date, workspace))
    return sorted(result, key=lambda entry: entry["date"])


def date_by_key(key, day, workspace=None) -> dict | None:
    """The special date ``key`` names (an entry's ``key``: ``b:<slug>`` or ``d:<id>``) on
    ``day``, as special_dates_between gives it; None when it is not one of the dates
    ``workspace`` sees that day."""
    return next((entry for entry in special_dates_between(day, day, workspace) if entry["key"] == key), None)


def get_holidays_for_range(start_date, end_date, workspace=None):
    """Special dates in the range, keyed by ISO date (several can share a day)."""
    result: dict[str, list[dict]] = {}
    for entry in special_dates_between(start_date, end_date, workspace):
        result.setdefault(entry["date"].isoformat(), []).append(entry)
    return result
