@admin_only
async def sudogrant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant sudo access to a user"""
    if not context.args:
        return await update.message.reply_text("Usage: /sudogrant <user_id>")
    
    try:
        user_id = int(context.args[0])
    except:
        return await update.message.reply_text("❌ Invalid user ID")
    
    # Check if already has sudo
    existing = sudo_col.find_one({"user_id": user_id})
    if existing:
        return await update.message.reply_text(f"⚠️ User {user_id} already has sudo access")
    
    # Grant sudo
    sudo_col.insert_one({
        "user_id": user_id,
        "granted_by": update.effective_user.id,
        "granted_at": datetime.now().isoformat()
    })
    
    await update.message.reply_text(f"✅ Sudo access granted to user {user_id}")


@admin_only
async def sudarevoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revoke sudo access from a user"""
    if not context.args:
        return await update.message.reply_text("Usage: /sudarevoke <user_id>")
    
    try:
        user_id = int(context.args[0])
    except:
        return await update.message.reply_text("❌ Invalid user ID")
    
    result = sudo_col.delete_one({"user_id": user_id})
    
    if result.deleted_count > 0:
        await update.message.reply_text(f"✅ Sudo access revoked from user {user_id}")
    else:
        await update.message.reply_text(f"❌ User {user_id} doesn't have sudo access")


@admin_only
async def sudolist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all users with sudo access"""
    sudo_users = list(sudo_col.find())
    
    if not sudo_users:
        return await update.message.reply_text("📭 No sudo users found")
    
    text = "👤 <b>SUDO USERS:</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
    for su in sudo_users:
        user_id = su.get("user_id")
        granted_by = su.get("granted_by")
        granted_at = su.get("granted_at", "?")[:10]  # Just date
        text += f"🔐 User: <code>{user_id}</code>\n"
        text += f"   Granted by: {granted_by}\n"
        text += f"   Since: {granted_at}\n\n"
    
    await update.message.reply_text(text, parse_mode="HTML")
