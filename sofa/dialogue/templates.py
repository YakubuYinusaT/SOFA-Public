"""Reply templates. Facts come from the database; wording comes from here (or, for whitelisted
clarification turns in `generated` languages, from N-ATLaS via placeholders + validator).

Placeholders use {name} or {obj.field} and are filled by validator.fill().

ENGLISH is written here. Yoruba, Hausa and Igbo live in translations.json, produced by
scripts/import_translations.py from the translator's spreadsheet (scripts/export_translation_sheet.py).
A key with no translation falls back to English, and is then SPOKEN with the English voice
(speak_lang), so a Yoruba caller never hears English words read by a Yoruba TTS voice.
"""

import json
import logging
from pathlib import Path

from .validator import PLACEHOLDER, fill

log = logging.getLogger("sofa.templates")

# key -> phrasings, rotated by turn number so the same wording is not repeated.
EN: dict[str, list[str]] = {
    # openings
    "daypart_morning": ["Good morning"],
    "daypart_afternoon": ["Good afternoon"],
    "daypart_evening": ["Good evening"],
    "greet_new": ["{greeting}, this is Sofa from {merchant}. Who am I speaking with, and what would you like today?"],
    "greet_returning": ["Welcome back, {name}. What would you like today?"],
    "greet_returning_repeat": ["Welcome back, {name}. Same as last time, {last_order}?"],
    # the front desk: SOFA itself answers, not a shop. Rotated at random per call so it never sounds scripted.
    "greet_gateway": [
        "{greeting}, this is Sofa, your personal assistant. What can I help you with?",
        "{greeting}! Sofa here, your personal assistant. How can I help you today?",
        "{greeting}, you have reached Sofa, your personal assistant. What do you need help with?",
        "Hello, this is Sofa, your personal assistant. What would you like me to do for you?",
    ],
    "greet_gateway_returning": [
        "{greeting}, {name}. It is Sofa. What can I help you with?",
        "{greeting}, {name}! Sofa here. What do you need today?",
        "Welcome back, {name}. How can I help you today?",
    ],
    "greet_gateway_returning_bank": [
        "{greeting}, {name}. Would you like to check your balance, send money, or something else?",
        "Welcome back, {name}. Is it your account today, or something else?",
    ],
    "gateway_ask_again": [
        "Sorry, I did not quite get what you need. You can ask me about your bank, buy something, or look something up.",
        "I am sorry, could you tell me again what you need help with? For example your bank, shopping, or a quick search.",
    ],
    "lookup_ask": ["What would you like me to look up?"],
    "lookup_answer": ["{ack}. {answer} Is there anything else I can help with?"],
    "lookup_none": ["I am sorry, I could not find a good answer to that. Would you like to ask it another way?"],
    "lookup_unavailable": ["I am sorry, I cannot search right now. Is there anything else I can help with?"],
    "service_off": ["Sorry, I cannot help with {service} on this line. I can help with {offered}. What would you like to do?"],
    "gateway_not_ready": ["{ack}. I understood you want help with {service}. That part of Sofa is still being connected. Is there anything else I can help with?"],
    "gateway_no_human": ["I am an AI assistant, so I cannot put you through to a person myself, but I can pass your request to the right service to follow up. Is it your bank, shopping, or something else?"],
    "gateway_give_up": ["I am sorry, I could not get that. I can help with your bank, shopping, or a search. What do you need?"],
    "gateway_handoff_provider": ["I am sorry I could not finish that. I will pass the details to {provider}, and they will follow up with you. Is there anything else I can help with?"],
    # banking: finding which bank the caller means
    "bank_none_linked": ["I could not find any bank under your number. Would you like me to connect you with one? Or tell me if you would like to open a new account."],
    "bank_none_linked_named": ["I could not find a {bank} account under your number. Would you like me to connect you with {bank}?"],
    "bank_link_which": ["I can connect you with {banks}. Which one would you like?"],
    "bank_link_requested": ["{ack}. I will pass your details to {bank} so they can set you up, and they will follow up with you. Is there anything else I can help with?"],
    "bank_link_pending": ["Your request to {bank} has already been passed on, and they will follow up with you. Is there anything else I can help with?"],
    "bank_confirm_one": ["I found your {bank} account. Shall I go ahead with {bank}?"],
    "bank_choose": ["I found accounts with {banks}. Which one would you like to use?"],
    "bank_selected": ["{ack}. We are on {bank}. What would you like to do?"],
    "bank_ask_what": ["What would you like to do with your {bank} account? For example your balance, a transfer, airtime or a bill."],
    # banking: asking for what is missing, reading back, and the bank's answers (every figure comes from the bank)
    "bank_ask_amount_transfer": ["How much would you like to send?"],
    "bank_ask_amount_airtime": ["How much airtime would you like?"],
    "bank_ask_amount_bill": ["How much is the bill?"],
    "bank_ask_beneficiary": ["Who would you like to send it to?"],
    "bank_ask_biller": ["Which bill would you like to pay? For example electricity or cable TV."],
    "bank_ask_details": ["Please tell me what happened, and I will log it with {bank}."],
    "bank_beneficiary_which": ["I have more than one match. Which one do you mean: {people}?"],
    "bank_beneficiary_unknown": ["I cannot find {who} in your saved recipients. For now, new recipients can only be added in your bank's app or at a branch. Is there anything else I can help with?"],
    "bank_confirm_transfer": ["That is {amount} to {who}. Shall I go ahead?"],
    "bank_confirm_transfer_usual": ["That is {amount} to {who}, the same as last time. Shall I go ahead?"],
    "bank_confirm_airtime": ["That is {amount} airtime for this number. Shall I go ahead?"],
    "bank_confirm_bill": ["That is {amount} to {biller}. Shall I go ahead?"],
    "bank_confirm_card": ["I will block your card. Shall I go ahead?"],
    "bank_cancelled": ["No problem, I have not done anything. Is there anything else I can help with?"],
    "bank_balance": ["Your {bank} balance is {balance}. Is there anything else I can help with?"],
    "bank_transfer_done": ["Done. {amount} sent to {who}. Your balance is now {balance}. Is there anything else I can help with?"],
    "bank_airtime_done": ["Done. {amount} airtime has been sent to this number. Your balance is now {balance}. Is there anything else I can help with?"],
    "bank_bill_done": ["Done. {amount} paid to {biller}. Your balance is now {balance}. Is there anything else I can help with?"],
    "bank_insufficient": ["Sorry, your balance of {balance} is not enough for {amount}. Is there anything else I can help with?"],
    "bank_over_limit": ["Sorry, {amount} is above the {limit} limit for phone requests. Is there anything else I can help with?"],
    "bank_statement_sent": ["I have sent your last {number} transactions to your phone by SMS. Is there anything else I can help with?"],
    "bank_no_transactions": ["I do not see any transactions on your account yet. Is there anything else I can help with?"],
    "bank_last_transaction": ["Your last transaction was {summary}, and it was {state}. Is there anything else I can help with?"],
    "bank_card_blocked": ["Done. Your card ending {last4} is blocked. Is there anything else I can help with?"],
    "bank_card_none": ["I do not see a card on your account. Is there anything else I can help with?"],
    "bank_complaint_logged": ["I have logged your complaint with {bank}, reference {ref}. They will follow up with you. Is there anything else I can help with?"],
    "bank_product_info": ["{text} Is there anything else I can help with?"],
    "bank_no_account": ["I cannot find an account for you at {bank}. I will pass this to them, and they will follow up with you. Is there anything else I can help with?"],
    "bank_failed_handoff": ["Sorry, I could not complete that with {bank}. I will pass the details to them, and they will follow up with you. Is there anything else I can help with?"],
    # verifying the caller: PIN and codes are typed on the keypad, never said
    "verify_pin_entry": ["Before I open your {bank} account, please enter your PIN on your keypad, then press hash."],
    "verify_pin_stale": ["It has been a few minutes, so please enter your PIN again, then press hash."],
    "verify_pin_new_resource": ["That is a different part of your account, so please enter your PIN again, then press hash."],
    "verify_pin_authorise": ["To authorise this, please enter your PIN on your keypad, then press hash."],
    "verify_pin_random": ["Just to be sure it is still you, please enter your PIN, then press hash."],
    "verify_pin_retry": ["That PIN was not correct. Please try again, then press hash."],
    "verify_ask_otp": ["I have sent a code to your phone by SMS. Please enter it on your keypad, then press hash."],
    "verify_otp_retry": ["That code was not correct. Please try again, then press hash."],
    "verify_ok": ["{ack}. Thank you, you are verified."],
    "verify_use_keypad": ["Please use your keypad for that, then press hash. Never say your PIN or code out loud."],
    "verify_abandoned": ["No problem, I will leave it there. Is there anything else I can help with?"],
    "verify_locked": ["For your safety I cannot continue with {bank} right now. Please try again later. Is there anything else I can help with?"],
    "verify_failed_handoff": ["That was not right too many times, so for your safety I have stopped. I will let {bank} know, and they will follow up with you. Is there anything else I can help with?"],
    # commerce through the gateway: which shop
    "shop_which": ["Which shop would you like to order from? I can help with {shops}."],
    "shop_confirm_one": ["I can order from {shop} for you. Shall I go ahead?"],
    "shop_selected": ["{ack}. You are shopping at {shop}. What would you like?"],
    "shop_none_available": ["I do not have a shop connected yet. Is there anything else I can help with?"],
    "gateway_anything_else": ["Is there anything else I can help with?"],
    "tool_blocked": ["Sorry, I cannot do that yet. Is there anything else I can help with?"],
    # opening an account
    "onboard_which_bank": ["I can open an account for you with {banks}. Which one would you like?"],
    "onboard_cannot_bank": ["Sorry, I cannot open an account with {bank} by phone yet. Is there anything else I can help with?"],
    "onboard_already": ["You already have a {bank} account connected to this number. Is there anything else I can help with?"],
    "onboard_intro": ["I can help you open a {bank} account. I will ask a few questions and finish by texting you a secure link. {bank} receives your details directly, and I do not keep your BVN. Shall we start?"],
    "onboard_declined": ["No problem. If you change your mind, just tell me you want to open an account. Is there anything else I can help with?"],
    "onboard_ask_otp": ["First, I have sent a code by SMS to the number you are calling from, to make sure it is yours. Please enter it on your keypad, then press hash."],
    "onboard_otp_retry": ["That code was not correct. Please try again, then press hash."],
    "onboard_otp_failed": ["I could not confirm this number, so I will stop here. You can try again later. Is there anything else I can help with?"],
    "onboard_abandoned": ["No problem. You can finish later: just tell me you want to continue your application. Is there anything else I can help with?"],
    "onboard_ask_name": ["Thank you. What is your full name, as it is written on your ID?"],
    "onboard_ask_name_again": ["Sorry, let us try that again. What is your full name, as it is written on your ID?"],
    "onboard_confirm_name": ["I have {full_name}. Is that right?"],
    "onboard_ask_dob": ["Now please enter your date of birth on your keypad, as day, month and year, eight digits, then press hash."],
    "onboard_dob_retry": ["That is not a date I can use. Please enter day, month and year as eight digits, then press hash."],
    "onboard_confirm_dob": ["I have {date}. Is that right?"],
    "onboard_too_young": ["Sorry, you need to be at least 16 to use banking on Sofa. Is there anything else I can help with?"],
    "age_too_young_service": ["Sorry, Sofa is for people aged 12 and above, so I cannot help with this. Goodbye."],
    # connecting an account that already exists: phone code, then date of birth, BVN and account number on the keypad
    "link_intro": ["To connect your {bank} account, I will send a code to this phone, and then I need your date of birth, your BVN and your account number, all typed on your keypad. Shall we start?"],
    "link_ask_acct": ["Now please enter your 10 digit {bank} account number on your keypad, then press hash."],
    "link_acct_retry": ["That was not 10 digits. Please try again, then press hash."],
    "link_done": ["Thank you, your {bank} account is now connected. What would you like to do?"],
    "link_mismatch": ["I am sorry, {bank} says those details do not match. Let us try once more."],
    "link_gave_up": ["I am sorry, {bank} still could not match those details, so I will pass this to them and they will follow up with you. Is there anything else I can help with?"],
    "link_not_found": ["I am sorry, {bank} does not have an account with that number. Would you like to open an account with them?"],
    "onboard_ask_bvn": ["Now please enter your 11 digit BVN on your keypad, then press hash. I pass it straight to {bank} and I do not keep it."],
    "onboard_bvn_retry": ["That was not 11 digits. Please try again, then press hash."],
    "onboard_ask_address": ["What is your home address?"],
    "onboard_gave_up": ["I am sorry, I am still not getting that, so I will stop here. You can continue later by telling me you want to finish your application. Is there anything else I can help with?"],
    "onboard_submitted": ["Thank you. Your application is with {bank}, reference {ref}. I have sent a secure link to your phone by SMS, to take a photo of your ID and a selfie. {bank} will tell you the result by SMS. Is there anything else I can help with?"],
    "onboard_incomplete": ["Sorry, {bank} says some details are missing, so I will pass this to them and they will follow up with you. Is there anything else I can help with?"],
    "onboard_resume": ["Welcome back. Your application {ref} is not finished yet, so let us carry on."],
    "onboard_status_none": ["I do not see an application under your number. Would you like to open an account?"],
    "onboard_status_awaiting": ["Your application {ref} is waiting for your identity check. Shall I send the secure link again?"],
    "onboard_resent": ["Done. I have sent the secure link again. Is there anything else I can help with?"],
    "onboard_status_review": ["Your application {ref} is being reviewed by {bank}. They will tell you the result by SMS. Is there anything else I can help with?"],
    "onboard_status_approved": ["Good news. Your {bank} account is ready, and {bank} will tell you by SMS how to set your PIN. Is there anything else I can help with?"],
    "onboard_status_rejected": ["{bank} could not approve your application {ref}. I will pass the details to them, and they will follow up with you. Is there anything else I can help with?"],
    "bank_funding_sent": ["I have sent your account details to your phone by SMS. Is there anything else I can help with?"],
    "gateway_chat": ["I am here. What can I do for you?", "Good to hear from you. What would you like to do?", "Sure. What can I help you with?"],
    "gateway_still_there": ["Are you still there? Tell me whenever you are ready.", "I am still here if you need anything."],
    "gateway_goodbye_silence": ["I will let you go for now. Call me again any time you need me."],
    "greet_owner": ["Hello {name}. Tell me a stock change, for example add 20 cartons of Indomie, price 9500, or ask how today went."],
    "consent": ["Calls are recorded to process orders and improve the service."],
    "ask_again_language": ["Thank you. What would you like today?"],
    "language_ask": [
        "Sorry, I didn't catch your language. Would you like to speak {languages}?",
        "Sorry, which language would you like to speak, {languages}?",
    ],
    "lang_name_en": ["English"],
    "lang_name_yo": ["Yoruba"],
    "lang_name_ha": ["Hausa"],
    "lang_name_ig": ["Igbo"],
    "no_service": ["This number is not in service."],
    # small words and rotating acknowledgements
    "ack": ["Okay", "Got it", "Alright", "No problem"],
    "word_and": ["and"],
    "word_or": ["or"],
    # keeping the line alive
    "filler": ["One moment."],
    # a slow reply (a slow bank or network): one holding message after the other, in this order, then an apology
    "wait_1": ["Hold on while I retrieve the information."],
    "wait_2": ["I am still with you, please."],
    "wait_3": ["I am still working on it. Please give me a little more time."],
    "wait_4": ["It seems the network is slow, but I am still working on it."],
    "wait_5": ["Phew, it is finally coming. A few more seconds."],
    "late_reply_intro": ["Hello, this is Sofa. Sorry for keeping you waiting. Here is what you asked for."],
    "late_reply_pin": ["For your security, please enter your PIN on your keypad, then press hash, and I will give you the answer."],
    "late_reply_pin_wrong": ["That PIN was not right. Please try again, then press hash."],
    "late_reply_pin_stop": ["I could not confirm it is you, so I will not read it out. Please call me when you are ready and ask again."],
    "wait_giveup_sensitive": ["I am sorry, this is taking too long, and for your security I cannot call you back with an answer about your account. Please call me back in 30 minutes, and check your account before you try again."],
    "wait_giveup": ["I am sorry, this is taking longer than expected. I will call you back as soon as I have the answer."],
    "gateway_goodbye": ["Thank you for calling Sofa. Goodbye."],
    "call_time_limit": ["We have been on the line for a while, so I will let you go now. Please call me again any time. Goodbye."],
    "repeat_prompt": ["Sorry, I didn't catch that. Could you say it again?"],
    "still_there": ["Are you still there?"],
    "goodbye_silence": ["Okay, goodbye. We have sent our number by SMS."],
    "handoff": ["Sorry about that. I'll ask the owner to call you back shortly. Goodbye."],
    "declined": ["No problem. What would you like today?"],
    # ordering
    "ask_what": ["Nice to meet you, {name}. What would you like today?", "How can I help you today?"],
    "ask_more": ["What else would you like?"],
    # advice: the owner's own words are spoken first (as their own clip, always in English), then one of these
    "advice_offer": ["Would you like to order {product}?"],
    "advice_none": ["I do not have a recommendation for that, so I have asked our team to follow up with you. Is there anything else I can help with?"],
    "added": ["{ack}. {qty_unit} of {product}, that's {line_total}. Anything else?"],
    "added_many": ["{ack}. {items}, {total} so far. Anything else?"],
    "removed": ["{ack}. I removed {product}. Anything else?"],
    "changed": ["{ack}. {qty_unit} of {product}. Anything else?"],
    "clarify_variant": ["For that we have {options}. Which one?"],
    "clarify_unit": ["{ack}. Just a {u1}, or a {u2}?"],
    "clarify_unit_many": ["{ack}. Which one: {units}?"],
    "clarify_quantity": ["How many would you like?"],
    "not_available": ["Sorry, we don't have {spoken}. Anything else?"],
    "not_available_alt": ["Sorry, we don't have {spoken}. Would you like {alt_product} at {alt_price}?"],
    "out_of_stock": ["Sorry, {product} is out of stock right now. Anything else?"],
    "out_of_stock_alt": ["Sorry, {product} is out of stock. Would you like {alt_product} at {alt_price}?"],
    "not_enough_stock": ["Sorry, we only have {have_qty} {have_unit} of {product}. How many would you like?"],
    "avail_yes": ["Yes, we have {product}. It is {price} per {unit}."],
    "price_is": ["{product} is {price} per {unit}."],
    "empty_draft": ["You have no items yet. What would you like?"],
    # checkout
    "ask_address": ["Where should we deliver?"],
    "readback": ["So that's {items}. {total} in total, delivered to {address}. Should I place the order?"],
    "ask_change": ["No problem. What should I change?"],
    "order_placed_transfer": [
        "Done. I've sent the account number to this phone by SMS. Once you pay, we'll call you back to confirm. Thank you, {name}."
    ],
    "order_placed_pod": ["Done. You will pay on delivery. Thank you, {name}."],
    "cancelled": ["Done, your order is cancelled."],
    "nothing_to_cancel": ["I can't find an order to cancel."],
    # order and payment questions
    "track_none": ["I can't find an open order for you."],
    "track_status": ["Your order {status_phrase}."],
    "status_draft": ["is not placed yet"],
    "status_confirmed": ["is confirmed"],
    "status_awaiting_payment": ["is waiting for your payment"],
    "status_paid": ["is paid and being prepared"],
    "status_dispatched": ["is on its way"],
    "status_delivered": ["has been delivered"],
    "status_cancelled": ["was cancelled"],
    "payment_paid": ["Yes, we have received your payment of {amount}."],
    "payment_pending": ["Not yet. Please pay {amount} to the account we sent by SMS."],
    "payment_none": ["I can't find an unpaid order for you."],
    # owner stock updates
    "owner_readback": ["{change}. Should I apply it?"],
    "owner_done": ["Done. {product} is updated. Anything else?"],
    "owner_cancelled": ["Okay, no change made. Anything else?"],
    "owner_needs_web": ["That price change is large, please confirm it on the web page. Anything else?"],
    "owner_unclear": ["Which item, how many, and what price?"],
    "owner_more": ["Anything else?"],
    "report_orders": ["Today you have {orders} orders, {value} in total."],
    "report_one_order": ["Today you have one order, {value}."],
    "report_no_orders": ["No orders yet today."],
    "report_unpaid": ["{unpaid} are waiting for payment."],
    "report_one_unpaid": ["One is waiting for payment."],
    "report_missed": ["Callers also asked for {items}, which you did not have."],
    "report_callbacks": ["{count} callers are waiting for you to call them back."],
    "report_one_callback": ["One caller is waiting for you to call them back."],
    # calls we place to the customer
    "outbound_payment_received": ["Hello, this is Sofa from {merchant}. Your payment of {total} has been received, and your order is being prepared."],
    "outbound_dispatched": ["Hello, this is Sofa from {merchant}. Your order is on its way."],
    "outbound_delivered": ["Hello, this is Sofa from {merchant}. Your order has been delivered. Is there anything else you need?"],
}

# When each phrase is spoken: shown to translators.
CONTEXT: dict[str, str] = {
    "daypart_morning": "Greeting word used at the start of the call, before midday.",
    "daypart_afternoon": "Greeting word used from midday to 5 pm.",
    "daypart_evening": "Greeting word used after 5 pm.",
    "greet_new": "First words to a NEW caller. Sofa introduces herself, then asks their name AND what they want, in one go.",
    "greet_returning": "First words to a caller we know, who has no earlier order.",
    "greet_returning_repeat": "First words to a returning caller: offers the same order as last time. The last order is read out after 'Same as last time'.",
    "greet_gateway": "First words when someone calls SOFA itself, before we know what they need. Like a friendly customer-care agent: greet, say you are Sofa their personal assistant, ask how you can help. Give several different ways to say it; one is picked at random each call.",
    "greet_gateway_returning": "First words to a caller SOFA already knows by name. Same friendly tone; several variations are picked at random.",
    "greet_gateway_returning_bank": "First words to a caller SOFA knows by name who has a bank connected to their number. Friendly, offers the most common things (balance, sending money) without presuming. Several wordings are rotated.",
    "gateway_ask_again": "Sofa did not understand what the caller needs. Asks again politely and lists three examples: their bank, buying something, looking something up. Never blame the caller.",
    "lookup_ask": "The caller asked Sofa to look something up but did not say what. Asks what they would like looked up.",
    "lookup_answer": "Answers a general-knowledge question the caller asked. {answer} is the answer, already written. Then asks if there is anything else.",
    "lookup_none": "Sofa searched but found nothing useful. Apologises, never blames the caller, and offers to try the question another way.",
    "lookup_unavailable": "The search could not be reached just now. Apologises and asks if there is anything else Sofa can help with.",
    "service_off": "The caller asked for a service that is switched off on this line. {service} is what they asked for (for example your bank); {offered} lists what Sofa can do here (for example shopping or a search). Polite, then asks what they would like to do.",
    "gateway_not_ready": "The caller asked for a service that is not connected yet. {service} is the name of the service, for example your bank. Says it is still being connected and asks if there is anything else.",
    "gateway_no_human": "The caller asked for a person before SOFA knows which service they need. SOFA is an AI assistant, so it says so kindly, offers to pass the request to the right service to follow up, and asks which service: their bank, shopping, or something else.",
    "gateway_give_up": "Said when SOFA could not understand three times in a row and does not know which service the caller needs, so there is nobody to pass the request to. Apologises, lists what it can help with (bank, shopping, search) and asks what they need. The call carries on. Never blame the caller.",
    "gateway_handoff_provider": "Said when SOFA could not finish a request for a known service. {provider} is who deals with it, for example your bank or the shop. Apologises, says the details are passed to them and they will follow up with the caller, then asks if there is anything else it can help with. Do NOT promise a time or a call. The call carries on.",
    "bank_none_linked": "The caller wants their bank but SOFA finds no bank connected to their phone number (they are new, or have not activated one). Says so and offers to connect them with one. A yes/no question.",
    "bank_none_linked_named": "The caller named a bank but it is not connected to their number. {bank} is the bank they named. Offers to connect them with it. A yes/no question.",
    "bank_link_which": "The caller said yes to being connected to a bank. {banks} is the list of banks on offer, for example Demo Bank, GT Bank or Access Bank. Asks which one.",
    "bank_link_requested": "SOFA has recorded the request to connect the caller to {bank} and passed the details to that bank, who will follow up. Do NOT promise a time or a call. Then asks if there is anything else.",
    "bank_link_pending": "The caller had already asked to be connected to {bank} and it is still with the bank. Says it has been passed on and they will follow up. Then asks if there is anything else.",
    "bank_confirm_one": "The caller has exactly one bank connected. SOFA says which and asks them to confirm. A yes/no question.",
    "bank_choose": "The caller has several banks connected. {banks} lists them, for example Demo Bank or GT Bank. Asks which one to use.",
    "bank_selected": "The bank is chosen but the caller has not yet said what they want to do. Says which bank, and asks what they would like to do.",
    "bank_ask_what": "SOFA does not know what the caller wants to do at their bank. Asks, with examples: balance, a transfer, airtime, a bill.",
    "bank_ask_amount_transfer": "Money transfer: the caller did not say how much. Asks how much to send.",
    "bank_ask_amount_airtime": "Airtime purchase: the caller did not say how much. Asks how much airtime.",
    "bank_ask_amount_bill": "Bill payment: the caller did not say how much. Asks how much the bill is.",
    "bank_ask_beneficiary": "Money transfer: the caller did not say who to send to. Asks who.",
    "bank_ask_biller": "Bill payment: the caller did not say which bill. Asks which, with examples: electricity, cable TV.",
    "bank_ask_details": "A complaint: asks the caller to describe what happened, which will be logged with their bank. {bank} is the bank.",
    "bank_beneficiary_which": "The name the caller said matches more than one saved recipient. {people} lists them. Asks which one.",
    "bank_beneficiary_unknown": "The recipient the caller named is not in their saved recipients. {who} is the name they said. Says new recipients cannot be added by phone yet, then asks if there is anything else.",
    "bank_confirm_transfer": "READ-BACK before a transfer: the amount and the recipient, then asks for a yes. This is the main protection against mistakes, so keep it clear.",
    "bank_confirm_transfer_usual": "READ-BACK before a transfer when the caller said 'the usual': the amount and recipient from last time, and says it is the same as last time. Asks for a yes.",
    "bank_confirm_airtime": "READ-BACK before buying airtime for the caller's own number: the amount. Asks for a yes.",
    "bank_confirm_bill": "READ-BACK before paying a bill: the amount and which bill. Asks for a yes.",
    "bank_confirm_card": "READ-BACK before blocking the caller's card. Asks for a yes.",
    "bank_cancelled": "The caller said no or changed their mind: nothing was done. Says so, then asks if there is anything else.",
    "bank_balance": "The bank's answer to 'what is my balance'. {balance} is an amount of money, for example 42,500 naira. Then asks if there is anything else.",
    "bank_transfer_done": "The bank has sent the transfer. {amount} and the recipient {who}, then the new balance {balance}. Then asks if there is anything else.",
    "bank_airtime_done": "The bank has bought airtime for the caller's own number. Says the amount and the new balance. Then asks if there is anything else.",
    "bank_bill_done": "The bank has paid a bill. Says the amount, which bill ({biller}) and the new balance. Then asks if there is anything else.",
    "bank_insufficient": "The balance is too low. Says the balance and the amount that was asked for, kindly. Then asks if there is anything else.",
    "bank_over_limit": "The amount is above the bank's limit for requests by phone. {limit} is the limit. Says so kindly, then asks if there is anything else.",
    "bank_statement_sent": "The last few transactions were sent to the caller's phone by SMS. {number} is how many, for example 5. Then asks if there is anything else.",
    "bank_no_transactions": "The account has no transactions yet. Says so, then asks if there is anything else.",
    "bank_last_transaction": "The bank's answer about the last transaction. {summary} is for example 5,000 naira to Hauwa Bello, {state} is its state, for example successful. Then asks if there is anything else.",
    "bank_card_blocked": "The card is blocked. {last4} is the last four digits. Then asks if there is anything else.",
    "bank_card_none": "There is no card on the account to block. Then asks if there is anything else.",
    "bank_complaint_logged": "The complaint is logged with the bank. {ref} is the reference, said as given. The bank will follow up; do NOT promise a time. Then asks if there is anything else.",
    "bank_product_info": "Information from the bank about fees or requirements. {text} is the bank's own wording. Then asks if there is anything else.",
    "bank_no_account": "The bank has no account for this caller. {bank} is the bank. Says it will pass this to them and they will follow up, then asks if there is anything else.",
    "bank_failed_handoff": "The bank could not be reached or failed. {bank} is the bank. Apologises, says the details are passed to them and they will follow up, then asks if there is anything else.",
    "verify_pin_entry": "FIRST security check of a banking call. The caller must type their PIN on the phone keypad and press hash. Never ask them to say it. {bank} is their bank.",
    "verify_pin_stale": "Security check: some minutes have passed since the last PIN. Asks for the PIN again on the keypad, hash to finish.",
    "verify_pin_new_resource": "Security check: the caller moved to a different, more sensitive part of their account (for example from balance to transfers). Asks for the PIN again on the keypad.",
    "verify_pin_authorise": "FINAL security check: the caller has just heard the details of a transfer, payment or card block and said yes out loud. Asks for the PIN on the keypad to authorise it. Never ask them to say it.",
    "verify_pin_random": "Random security check, so nobody can rely on a pattern. Friendly, short: just making sure it is still them. Asks for the PIN on the keypad.",
    "verify_pin_retry": "The PIN typed was wrong. Says so without blame and asks to try again on the keypad.",
    "verify_ask_otp": "After the PIN was right: a one-time code was sent by SMS. Asks the caller to type it on the keypad and press hash.",
    "verify_otp_retry": "The code typed was wrong. Says so without blame and asks to try again on the keypad.",
    "verify_ok": "The caller passed verification. Thanks them briefly. It is followed by the answer to what they asked.",
    "verify_use_keypad": "The caller SPOKE while SOFA was waiting for a PIN or code. Reminds them to use the keypad and never say it out loud.",
    "verify_abandoned": "Nothing was typed in time, so SOFA stops asking. Calm, no blame. Asks if there is anything else it can help with.",
    "verify_locked": "Too many wrong attempts earlier, so the account is paused for a while. {bank} is their bank. Says it kindly, asks them to try later, and asks if there is anything else.",
    "verify_failed_handoff": "Too many wrong attempts in this call. {bank} is their bank. Says SOFA has stopped for their safety, will let the bank know, and the bank will follow up. Do NOT promise a time. Asks if there is anything else.",
    "shop_which": "The caller wants to buy something but did not say from which shop, and several shops are connected. {shops} lists them, for example CI Store or Mama Put Provisions. Asks which.",
    "shop_confirm_one": "The caller wants to buy something and only one shop is connected. {shop} is its name. Offers to order from it. A yes/no question.",
    "shop_selected": "The shop is chosen. {shop} is its name. Says the caller is shopping there and asks what they would like.",
    "shop_none_available": "The caller wants to buy something but no shop is connected. Says so, then asks if there is anything else.",
    "gateway_anything_else": "Said right after a task is finished (for example an order is placed), because SOFA never ends a call itself. Asks if there is anything else it can help with.",
    "tool_blocked": "SOFA is not allowed to do what the caller asked yet, for example a step that needs verification first. Apologises briefly and asks if there is anything else.",
    "onboard_which_bank": "The caller wants to open an account and several banks are available. {banks} lists them. Asks which.",
    "onboard_cannot_bank": "The caller named a bank that cannot open accounts by phone yet. {bank} is the bank. Says so kindly, then asks if there is anything else.",
    "onboard_already": "The caller wants to open an account but already has one at this bank connected to their number. Says so, then asks if there is anything else.",
    "onboard_intro": "Start of opening an account. Says SOFA will ask a few questions and finish by texting a secure link, that the bank receives the details directly, and that SOFA does not keep the BVN. Asks if they want to start. A yes/no question.",
    "onboard_declined": "The caller does not want to open an account now. Kind, no pressure. Then asks if there is anything else.",
    "onboard_ask_otp": "SECURITY: a code was just sent by SMS to the phone number the caller is using, to prove the number is theirs. Asks them to type it on the keypad and press hash. Never ask them to say it.",
    "onboard_otp_retry": "The code typed was wrong. Says so without blame and asks to try again on the keypad.",
    "onboard_otp_failed": "The phone number could not be confirmed after several tries, so SOFA stops. Kind. Then asks if there is anything else.",
    "onboard_abandoned": "Nothing was typed in time. SOFA stops asking; the application stays saved and can be continued later. Calm, no blame. Then asks if there is anything else.",
    "onboard_ask_name": "Asks for the caller's full name as it is written on their ID.",
    "onboard_ask_name_again": "The name was not right. Asks again for the full name as written on their ID, without blame.",
    "onboard_confirm_name": "Reads back the name SOFA heard. {full_name} is the name. Asks if it is right. A yes/no question.",
    "onboard_ask_dob": "Asks the caller to type their date of birth on the keypad as day, month and year, eight digits, then hash.",
    "onboard_dob_retry": "The date typed was not usable. Asks again, eight digits: day, month, year.",
    "onboard_confirm_dob": "Reads back the date of birth typed. {date} is the date, for example 12 March 1990. Asks if it is right. A yes/no question.",
    "onboard_too_young": "The caller is under 16, so they cannot use banking on Sofa. Kind. Then asks if there is anything else.",
    "age_too_young_service": "The caller is under 12, and Sofa is only for people aged 12 and above. Kind and short. The call ends after it.",
    "link_intro": "The caller wants to connect an account they already have with {bank}. Says a code is sent to this phone, then they type their date of birth, BVN and account number on the keypad. Asks if they want to start. A yes/no question.",
    "link_ask_acct": "SECURITY: asks the caller to type their 10 digit {bank} account number on the keypad, then hash.",
    "link_acct_retry": "The account number typed was not 10 digits. Asks again on the keypad.",
    "link_done": "The account is now connected. {bank} is the bank. Thanks, then asks what they would like to do.",
    "link_mismatch": "The bank says the details typed do not match its records. Kind, never accusing. Says to try once more. {bank} is the bank.",
    "link_gave_up": "The details still did not match after a second try. Sofa passes it to {bank}, who will follow up. Then asks if there is anything else.",
    "link_not_found": "The bank has no account with the number typed. Asks if the caller would like to open an account with {bank}. A yes/no question.",
    "onboard_ask_bvn": "SECURITY: asks the caller to type their 11 digit BVN on the keypad, then hash, and says it goes straight to the bank and SOFA does not keep it. {bank} is the bank.",
    "onboard_bvn_retry": "The BVN typed was not 11 digits. Asks again on the keypad.",
    "onboard_ask_address": "Asks for the caller's home address.",
    "onboard_gave_up": "SOFA could not understand the answer after trying twice. Stops kindly, says the application is saved and can be finished later, then asks if there is anything else.",
    "onboard_submitted": "The application is sent. {bank} is the bank, {ref} the reference. Says a secure link was sent by SMS for a photo of their ID and a selfie, and the bank will send the result by SMS. Do NOT promise a time. Then asks if there is anything else.",
    "onboard_incomplete": "The bank says details are missing. Says SOFA passes this to the bank and they will follow up. Then asks if there is anything else.",
    "onboard_resume": "The caller is continuing an unfinished application. {ref} is its reference. Welcomes them back and says they will carry on. A question follows this sentence.",
    "onboard_status_none": "The caller asked about their application but there is none under their number. Asks if they want to open an account. A yes/no question.",
    "onboard_status_awaiting": "The application is waiting for the caller's identity check by the secure link. {ref} is the reference. Offers to send the link again. A yes/no question.",
    "onboard_resent": "The secure link was sent again by SMS. Then asks if there is anything else.",
    "onboard_status_review": "The application is being reviewed by the bank. {ref} is the reference. The bank will send the result by SMS; do NOT promise a time. Then asks if there is anything else.",
    "onboard_status_approved": "The account is open. Says it is ready and the bank will text how to set the PIN. Then asks if there is anything else.",
    "onboard_status_rejected": "The bank could not approve the application. {ref} is the reference. Says SOFA passes the details to the bank, who will follow up. Kind. Then asks if there is anything else.",
    "bank_funding_sent": "The account number for funding was sent to the caller's phone by SMS (it is not said aloud). Then asks if there is anything else.",
    "gateway_chat": "The caller said a greeting, thanks or made small talk, with no request in it. Reply warmly and briefly to what they actually said (hello back, you are welcome, and so on), then ask what they need or whether there is anything else. Several wordings are rotated.",
    "gateway_still_there": "Said when the caller has been silent. Gentle, never impatient. Two wordings, used in turn.",
    "gateway_goodbye_silence": "After several silences in a row, when nobody is answering: Sofa lets the caller go and invites them to phone again.",
    "greet_owner": "First words when the shop OWNER calls their own number to change stock or prices.",
    "consent": "Recording notice, played once per customer after their first reply. Must be short and clear.",
    "ask_again_language": "Said right after a caller picks a language by pressing a number.",
    "language_ask": "Asked out loud, ONCE, when Sofa could not tell which language the caller is speaking. It is played in English first, then in your language, so a caller who does not follow English still understands. The caller answers by speaking. Use a natural, polite question.",
    "lang_name_en": "The name of the English language, in your language, as a customer would say it (for example the Yoruba word for English). Used inside the question above.",
    "lang_name_yo": "The name of the Yoruba language, in your language. Used inside the question above.",
    "lang_name_ha": "The name of the Hausa language, in your language. Used inside the question above.",
    "lang_name_ig": "The name of the Igbo language, in your language. Used inside the question above.",
    "no_service": "Said when someone calls a number that is not set up. Always English; not translated.",
    "ack": "Short acknowledgements said before an answer. Rotated so Sofa never says the same one twice in a row. Give up to 4.",
    "word_and": "The word 'and' used to join items in a list (two cartons and three tins).",
    "word_or": "The word 'or' used to join choices (the tin or the sachet).",
    "filler": "Played when Sofa needs a moment to think.",
    "wait_1": "Second holding message on a slow reply, said while Sofa fetches information. Warm and calm.",
    "wait_2": "Third holding message: tells the caller Sofa has not left the call. Warm and reassuring.",
    "wait_3": "Fourth holding message: still working, asks for a little more patience.",
    "wait_4": "Fifth holding message: says the network seems slow but Sofa is still working on it.",
    "wait_5": "Sixth holding message: relief that the answer is nearly here, a few more seconds.",
    "late_reply_intro": "First words when Sofa phones the caller back because the answer came after they had been told it was taking too long. Apologetic and warm.",
    "late_reply_pin": "On the call back, before Sofa gives the late answer: asks the caller to type their PIN on the keypad, then hash. Polite and short.",
    "late_reply_pin_wrong": "On the call back: the PIN typed was wrong, asks for it again. Never blames the caller.",
    "late_reply_pin_stop": "On the call back: too many wrong PINs, so Sofa will not read out the answer and asks the caller to phone in. Calm, never accusing.",
    "wait_giveup_sensitive": "Said when an answer about the caller's bank account took too long. Sofa cannot phone back about an account, so it asks the caller to call back in 30 minutes and to check their account before trying the same thing again. Apologetic, calm.",
    "wait_giveup": "Said when the answer is taking too long: Sofa apologises and promises to phone the caller back as soon as it has the answer. Never blames the caller. Is followed by 'Is there anything else I can help with?'",
    "gateway_goodbye": "Said when the caller says they need nothing else, or says goodbye. A warm, short thanks and goodbye. The call ends after it.",
    "call_time_limit": "Said when a call has gone on for a long time (eight minutes) and Sofa has to end it. Polite, not abrupt: invites the caller to call again any time.",
    "repeat_prompt": "Asks the caller to repeat when Sofa did not understand. Must be polite, never blame the caller.",
    "still_there": "After 5 seconds of silence.",
    "goodbye_silence": "After a second silence: Sofa says goodbye. An SMS with the shop number is also sent.",
    "handoff": "Said when Sofa cannot help and the shop owner will phone the caller back. The call then ends.",
    "declined": "Said when the caller does not want the offered repeat order.",
    "ask_what": "Said after the caller gives their name, or when Sofa did not hear an item.",
    "ask_more": "Caller said yes to 'anything else?': asks what else they want.",
    "advice_offer": "Said right after the shop owner's advice about a product. Asks if the caller would like to order {product}. A yes/no question.",
    "advice_none": "The caller asked how or when to use something and the shop has no advice written for it. Says there is no recommendation, that the shop team has been asked to follow up, then asks if there is anything else.",
    "added": "Confirms ONE item just added to the order, then asks if they want anything else.",
    "added_many": "Confirms several items added at once, with the running total.",
    "removed": "Confirms an item was removed from the order.",
    "changed": "Confirms a quantity was changed.",
    "clarify_variant": "The caller's words matched two or three products. Sofa names the real options with prices and asks which one.",
    "clarify_unit": "Caller did not say how they want the item (for example a pack or a carton). Asks with two choices.",
    "clarify_unit_many": "Same as above but with three or more choices.",
    "clarify_quantity": "Caller did not say how many.",
    "not_available": "The shop does not sell what the caller asked for.",
    "not_available_alt": "The shop does not sell that, but offers a similar product with its price.",
    "out_of_stock": "The item exists but is out of stock.",
    "out_of_stock_alt": "Out of stock, and offers the closest alternative with its price.",
    "not_enough_stock": "There is some stock but less than the caller asked for.",
    "avail_yes": "Answer to 'do you have X?' when it is in stock.",
    "price_is": "Answer to 'how much is X?'.",
    "empty_draft": "Caller tried to check out with nothing in the order.",
    "ask_address": "Asks for the delivery address (asked only when we do not have one on file).",
    "readback": "READ-BACK before placing the order: every item, the total and the address, then asks for a yes. This is the main protection against mistakes, so keep it clear.",
    "ask_change": "Caller said the read-back was wrong: asks what to change.",
    "order_placed_transfer": "Order placed; the customer will pay by bank transfer. Says the account number was sent by SMS and Sofa will phone back after payment.",
    "order_placed_pod": "Order placed; the customer pays when the goods arrive.",
    "cancelled": "Confirms the order was cancelled.",
    "nothing_to_cancel": "Caller asked to cancel but there is no order.",
    "track_none": "Caller asked 'where is my order?' but there is no order.",
    "track_status": "Answer to 'where is my order?'. The status words come from the status_* phrases below.",
    "status_draft": "Finishes the sentence 'Your order ...'. Not placed yet.",
    "status_confirmed": "Finishes the sentence 'Your order ...'. Confirmed.",
    "status_awaiting_payment": "Finishes the sentence 'Your order ...'. Waiting for the customer's payment.",
    "status_paid": "Finishes the sentence 'Your order ...'. Paid, being prepared.",
    "status_dispatched": "Finishes the sentence 'Your order ...'. On its way.",
    "status_delivered": "Finishes the sentence 'Your order ...'. Delivered.",
    "status_cancelled": "Finishes the sentence 'Your order ...'. Cancelled.",
    "payment_paid": "Answer to 'did you receive my payment?': yes.",
    "payment_pending": "Answer to 'did you receive my payment?': not yet.",
    "payment_none": "Caller asked about a payment but has no unpaid order.",
    "owner_readback": "Owner stock update: reads back the change before applying it. The change itself is read out in English.",
    "owner_done": "Owner stock update applied.",
    "owner_cancelled": "Owner said no to the change.",
    "owner_needs_web": "A price change above 50 percent is not allowed by voice.",
    "owner_unclear": "Owner stock update: asks for the missing details.",
    "owner_more": "Owner asked how today went and heard the report: asks if they need anything else.",
    "report_orders": "Daily report read to the owner: how many orders came in today and their total value. Follows the same phrase for no orders.",
    "report_one_order": "Daily report read to the owner: EXACTLY ONE order came in today, and its value. (Used instead of the phrase above when the count is 1.)",
    "report_no_orders": "Daily report read to the owner: there were no orders yet today.",
    "report_unpaid": "Daily report read to the owner: how many orders are still waiting for the customer to pay.",
    "report_one_unpaid": "Daily report read to the owner: exactly ONE order is waiting for payment. (Used instead of the phrase above when the count is 1.)",
    "report_missed": "Daily report read to the owner: the items callers asked for that the shop does not have (one to three product names).",
    "report_callbacks": "Daily report read to the owner: how many callers are waiting for the owner to phone them back.",
    "report_one_callback": "Daily report read to the owner: exactly ONE caller is waiting for a call back. (Used instead of the phrase above when the count is 1.)",
    "outbound_payment_received": "Sofa PHONES the customer right after their payment arrives. First words of the call: who is calling, then the good news.",
    "outbound_dispatched": "Sofa phones the customer when the shop sends the order out for delivery.",
    "outbound_delivered": "Sofa phones the customer after delivery, and ends with a question so the customer can order more or report a problem.",
}

# P1 = spoken on almost every call: translate these first. P2 = rarer.
P1 = {
    "daypart_morning", "daypart_afternoon", "daypart_evening", "greet_gateway", "greet_gateway_returning", "greet_gateway_returning_bank", "gateway_ask_again", "greet_new", "greet_returning",
    "greet_returning_repeat", "consent", "ack", "word_and", "word_or", "repeat_prompt", "handoff",
    "ask_what", "ask_more", "added", "added_many", "clarify_variant", "clarify_unit", "clarify_unit_many",
    "clarify_quantity", "not_available", "out_of_stock", "avail_yes", "price_is", "ask_address",
    "language_ask", "lang_name_en", "lang_name_yo", "readback", "order_placed_transfer", "order_placed_pod", "still_there", "goodbye_silence",
    "track_status", "status_confirmed", "status_awaiting_payment", "status_paid", "status_dispatched",
    "status_delivered", "payment_paid", "payment_pending",
}
NOT_TRANSLATED = {"no_service"}  # spoken in English by design

# What each {placeholder} contains, so translators know what will be filled in.
PLACEHOLDER_HELP: dict[str, str] = {
    "languages": "the languages on offer, for example English or Yoruba (each name comes from the lang_name_* phrases)",
    "name": "the customer's first name, for example Musa",
    "answer": "the answer to a general question, already written in English from a web search, for example The capital of Ghana is Accra. Keep it as it is",
    "who": "a person's name as saved by the bank, for example Hauwa Bello",
    "people": "names joined with the word for 'or', for example Hauwa Bello or Hauwa Musa",
    "balance": "an amount of money, for example 42,500 naira",
    "limit": "an amount of money, for example 100,000 naira",
    "number": "a small count, for example 5",
    "ref": "a reference code, read out as given, for example ZB4F2A9C1D",
    "last4": "the last four digits of a card, for example 4321",
    "summary": "a transaction in a few words, for example 5,000 naira to Hauwa Bello",
    "state": "a transaction state, for example successful",
    "biller": "what a bill is for, for example electricity or cable TV",
    "text": "a sentence of information from the bank, kept as given",
    "shop": "a shop name, for example CI Store",
    "shops": "shop names joined with the word for 'or', for example CI Store or Mama Put Provisions",
    "full_name": "the caller's full name as they said it, for example Amina Yusuf",
    "date": "a date, for example 12 March 1990",
    "bank": "a bank name, for example Demo Bank",
    "banks": "bank names joined with the word for 'or', for example Demo Bank, GT Bank or Access Bank",
    "provider": "who follows up on the request: your bank, or the shop",
    "service": "the name of a service SOFA can connect to, for example your bank or shopping",
    "offered": "the services Sofa can help with on this line, joined into a list, for example shopping and a search",
    "merchant": "the shop name, for example CI Store",
    "greeting": "a greeting word from the daypart_* phrases, for example Good morning",
    "ack": "one of the short acknowledgements (Okay, Got it ...)",
    "last_order": "the customer's last order read out, for example 2 cartons of Indomie Super Pack",
    "qty_unit": "a quantity with its unit, for example 2 cartons or 3 tins",
    "product": "a product name, for example Indomie Super Pack",
    "line_total": "a price for one line, for example 14,000 naira",
    "items": "the order items, for example 2 cartons of Indomie Super Pack and 3 tins of Peak Milk Tin",
    "total": "the order total, for example 16,400 naira",
    "options": "two or three products with prices, for example Indomie Super Pack at 350 naira or Indomie Hungry Man at 500 naira",
    "u1": "a unit word, for example pack",
    "u2": "a unit word, for example carton",
    "units": "a list of unit words, for example pack, carton or crate",
    "spoken": "the words the caller used for a product we do not sell",
    "alt_product": "an alternative product name",
    "alt_price": "the alternative's price, for example 850 naira",
    "have_qty": "a number, how many are in stock",
    "have_unit": "a unit word in plural, for example tins",
    "price": "a price, for example 800 naira",
    "unit": "a unit word, for example tin",
    "address": "the delivery address exactly as the customer said it",
    "status_phrase": "one of the status_* phrases, for example is on its way",
    "amount": "an amount of money, for example 16,400 naira",
    "change": "the stock change read out in English, for example Add 20 cartons of Indomie Super Pack at 7,500 naira per carton",
    "orders": "a number of orders, for example 12",
    "value": "an amount of money, for example 184,500 naira",
    "unpaid": "a number of orders, for example 3",
    "count": "a number of callers, for example 2",
}

TRANSLATIONS_PATH = Path(__file__).with_name("translations.json")
LANGS = ("yo", "ha", "ig")
TRANSLATIONS: dict[str, dict[str, list[str]]] = {l: {} for l in LANGS}


def load_translations(path: Path | None = None) -> None:
    """(Re)load translations.json into TRANSLATIONS. Missing file means English only."""
    TRANSLATIONS.clear()
    TRANSLATIONS.update({l: {} for l in LANGS})
    path = path or TRANSLATIONS_PATH
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for lang, table in data.items():
        if lang in TRANSLATIONS:
            TRANSLATIONS[lang] = {k: [v for v in vs if v.strip()] for k, vs in table.items() if k in EN}


load_translations()


def has(key: str, lang: str) -> bool:
    if lang == "en":
        return True
    if key in NOT_TRANSLATED:
        return False  # always spoken in English
    return bool(TRANSLATIONS.get(lang, {}).get(key))


def speak_lang(key: str, lang: str) -> str:
    """Language whose TTS voice should read this key: the caller's if we have their wording, else English."""
    return lang if has(key, lang) else "en"


def variants(key: str, lang: str) -> list[str]:
    return TRANSLATIONS.get(lang, {}).get(key) or EN[key]


def pick(key: str, lang: str, variant: int = 0) -> str:
    v = variants(key, lang)
    return v[variant % len(v)]


def render(key: str, lang: str, facts: dict, variant: int = 0) -> str:
    return fill(pick(key, lang, variant), facts)


# Money and digits are spoken in English in every language: nearly everyone understands "ten thousand naira" the same way,
# and it avoids a voice model mangling numbers it was not trained on. Names and ordinary words stay in the caller's language.
VALUE_KEYS = {"amount", "balance", "limit", "line_total", "total", "price", "alt_price", "value", "last4", "number", "ref", "people", "date"}


def spell_digits(digits: str) -> str:
    """'4321' -> '4 3 2 1': account and card digits are read one at a time."""
    return " ".join(str(digits))


def value_parts(raw: str, facts: dict, lang: str) -> list[tuple[str, str]] | None:
    """A reply as (text, voice language) pieces, with each money value or digit group in the English voice. None when
    the reply is already English or has no such value (one clip is enough)."""
    if lang == "en":
        return None
    parts: list[tuple[str, str]] = []

    def flush(text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not any(c.isalnum() for c in text) and parts:  # lone punctuation joins the clip before it, never a clip of its own
            parts[-1] = (parts[-1][0] + text, parts[-1][1])
        elif any(c.isalnum() for c in text):
            parts.append((text, lang))

    buf, pos, found = "", 0, False
    for m in PLACEHOLDER.finditer(raw):
        buf += raw[pos:m.start()]
        pos = m.end()
        value = fill(m.group(0), facts)
        if m.group(1) in VALUE_KEYS:
            found = True
            flush(buf)
            buf = ""
            parts.append((value.strip(), "en"))
        else:
            buf += value
    flush(buf + raw[pos:])
    return parts if found else None


def acks(lang: str) -> list[str]:
    return variants("ack", lang)


def join_list(parts: list[str], lang: str, word_key: str = "word_and") -> str:
    """'a', 'a and b', 'a, b and c' with the language's own word for and/or."""
    word = pick(word_key, lang)
    if len(parts) <= 1:
        return parts[0] if parts else ""
    if len(parts) == 2:
        return f"{parts[0]} {word} {parts[1]}"
    return ", ".join(parts[:-1]) + f" {word} " + parts[-1]


def translatable_keys() -> list[str]:
    return [k for k in EN if k not in NOT_TRANSLATED]


def coverage(lang: str) -> tuple[int, int, list[str]]:
    """(translated, total, missing keys) for the keys a translator is asked to do."""
    keys = translatable_keys()
    missing = [k for k in keys if not TRANSLATIONS.get(lang, {}).get(k)]
    return len(keys) - len(missing), len(keys), missing


def placeholders(text: str) -> set[str]:
    return {m.group(0) for m in PLACEHOLDER.finditer(text)}


def fixed_phrases() -> dict[str, str]:
    """English templates with no variables: pre-generate their audio at deploy time (scripts.warm_cache)."""
    out = {}
    for key, phrasings in EN.items():
        for i, p in enumerate(phrasings):
            if "{" not in p:
                out[f"{key}:{i}"] = p
    return out
