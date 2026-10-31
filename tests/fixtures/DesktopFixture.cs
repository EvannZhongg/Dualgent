using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

class DesktopFixture : Form {
    TextBox project = new TextBox { AccessibleName = "Project name", Left = 24, Top = 65, Width = 350 };
    TextBox owner = new TextBox { AccessibleName = "Owner", Left = 24, Top = 135, Width = 350 };
    TextBox receipt = new TextBox { AccessibleName = "Web receipt", Left = 24, Top = 205, Width = 350 };
    Label status = new Label { Left = 24, Top = 310, Width = 530, Height = 80, Text = "Ready for a local test." };
    string output;
    DesktopFixture(string path) {
        output = path; Text = "Dualgent desktop verification lab"; Width = 620; Height = 470;
        Font = new Font("Segoe UI", 11); StartPosition = FormStartPosition.CenterScreen;
        Controls.Add(new Label { Text = "Project name", Left = 24, Top = 32, Width = 350 });
        Controls.Add(new Label { Text = "Owner", Left = 24, Top = 104, Width = 350 });
        Controls.Add(new Label { Text = "Web receipt", Left = 24, Top = 174, Width = 350 });
        Controls.Add(project); Controls.Add(owner); Controls.Add(receipt); Controls.Add(status);
        Button preview = new Button { Text = "Preview record", AccessibleName = "Preview record", Left = 24, Top = 257, Width = 160, Height = 36 };
        Button save = new Button { Text = "Confirm save", AccessibleName = "Confirm save", Left = 205, Top = 257, Width = 160, Height = 36, Visible = false };
        preview.Click += (s,e) => {
            status.Text = "Review: " + project.Text + " | " + owner.Text + " | " + receipt.Text;
            save.Visible = true;
        };
        save.Click += (s,e) => {
            File.WriteAllText(output, project.Text + "\n" + owner.Text + "\n" + receipt.Text);
            status.Text = "Saved record: " + project.Text + " | " + owner.Text + " | " + receipt.Text;
            save.Visible = false;
        };
        Controls.Add(preview); Controls.Add(save);
    }
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles();
        Application.Run(new DesktopFixture(args[0]));
    }
}
