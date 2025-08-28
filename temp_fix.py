#!/usr/bin/env python3
"""
Temporary fix script to disable edit functionality until database migration is applied.
This will comment out all references to the new columns.
"""

import re

def apply_temp_fix():
    """Apply temporary fixes to app.py"""
    
    # Read the current app.py file
    with open('lms_automation/app.py', 'r') as f:
        content = f.read()
    
    # Replace problematic lines with safe defaults
    fixes = [
        # Fix edit_count references
        (r'can_edit = pick_token\.edit_count < 2', 'can_edit = False  # Temporarily disabled'),
        (r'edits_remaining = 2 - pick_token\.edit_count', 'edits_remaining = 0  # Temporarily disabled'),
        (r'can_edit=pick_token\.edit_count < 2', 'can_edit=False'),
        (r'edits_remaining=2 - pick_token\.edit_count', 'edits_remaining=0'),
    ]
    
    for pattern, replacement in fixes:
        content = re.sub(pattern, replacement, content)
    
    # Write back
    with open('lms_automation/app.py', 'w') as f:
        f.write(content)
    
    print("✅ Temporary fixes applied to app.py")

def apply_model_fix():
    """Apply temporary fixes to models.py"""
    
    with open('lms_automation/models.py', 'r') as f:
        content = f.read()
    
    # Comment out the new columns temporarily
    fixes = [
        (r'edit_count = db\.Column\(db\.Integer, default=0\)', '# edit_count = db.Column(db.Integer, default=0)  # Temporarily disabled'),
        (r'last_edited_at = db\.Column\(db\.DateTime, nullable=True\)', '# last_edited_at = db.Column(db.DateTime, nullable=True)  # Temporarily disabled'),
        (r'if self\.edit_count >= 2:', 'if False:  # self.edit_count >= 2: (temporarily disabled)'),
        (r'self\.edit_count \+= 1', '# self.edit_count += 1  # Temporarily disabled'),
        (r'if self\.edit_count >= 2:', 'if True:  # self.edit_count >= 2: (temporarily disabled)'),
        (r'PickToken\.edit_count < 2', 'True  # PickToken.edit_count < 2 (temporarily disabled)'),
    ]
    
    for pattern, replacement in fixes:
        content = re.sub(pattern, replacement, content)
    
    with open('lms_automation/models.py', 'w') as f:
        f.write(content)
    
    print("✅ Temporary fixes applied to models.py")

if __name__ == "__main__":
    apply_temp_fix()
    apply_model_fix()
    print("🔧 All temporary fixes applied. Your app should work now.")
    print("📝 Remember to apply the database migration and revert these changes later.")