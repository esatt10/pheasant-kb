<?php
namespace App;

use App\Models\User;
require_once __DIR__ . '/helpers.php';
require_once 'config/app.php';

// function ghost() {}
class App extends Base implements Renderable
{
    const VERSION = "1.0";

    public function render(int $id): string
    {
        $user = User::find($id);
        return $this->format($user);
    }
}

function helper() { return strtoupper("x"); }
